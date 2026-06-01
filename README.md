# ZC33S Active Sound System

Cabin-only exhaust burble/pop synthesiser for Suzuki Swift Sport ZC33S MHEV.
Reads CAN bus listen-only → detects driving events → plays synthesised pops
through 3D-printed exhaust-pipe resonator (8" DY200-9A subwoofer).

---

## Hardware wiring

### CAN tap
| CAN wire | Colour (ZC33S footwell harness) |
|---|---|
| CAN-H | White |
| CAN-L | Red |

### ESP32 ↔ MCP2515 (SPI)
| Signal | ESP32 GPIO | MCP2515 pin |
|---|---|---|
| SCK  | 18 | SCK |
| MOSI | 23 | SI  |
| MISO | 19 | SO  |
| CS   | 5  | CS  |
| INT  | 4  | INT (polled, not IRQ) |

**CRITICAL:** Remove the onboard 120 Ω terminator resistor from the MCP2515 module before connecting — the car bus is already terminated. The module crystal must be 8.000 MHz (matches CNF1/2/3 in `config.py`).

### ESP32 ↔ PCM5102A (I2S)
| Signal | ESP32 GPIO | PCM5102A pin |
|---|---|---|
| BCK  | 26 | BCK  |
| LRCK | 25 | LRCK |
| DIN  | 22 | DIN  |

PCM5102A notes:
- **XMT** → 3.3 V (always unmuted)
- **SCK** → GND or float (PCM5102A uses internal PLL from BCK)
- Power the module from 3.3 V (uses LDO on most breakout boards)

### PCM5102A → Amplifier (TPA3116D2)
Analog RCA/3.5 mm out of PCM5102A → AMP IN. Amp runs on 12 V from car.
Speaker: DY200-9A 8" inside 3D-printed exhaust-pipe resonator (trunk).

---

## Quick-start: sniffer mode

Use `sniffer.py` (not `main_soundsystem.py`) to capture raw CAN:

```bash
mpremote cp can_sniffer/firmware/mcp_driver.py  :/mcp_driver.py
mpremote cp can_sniffer/firmware/config.py       :/config.py
mpremote cp can_sniffer/firmware/sniffer.py      :/main.py
mpremote run can_sniffer/firmware/sniffer.py

# On PC (separate terminal):
python can_sniffer/pc/capture.py --port /dev/ttyUSB0 --scenario "04_liftoff"
```

Send `B` over serial to switch to SLCAN mode, then use `capture.py` to record.

---

## Signal decode → CSV

```bash
cd can_sniffer/pc
python decode.py ../firmware/logs/04_accel_to_second_gear_then_liftoff.log
# → sound_sim/timelines/04_accel_to_second_gear_then_liftoff.csv
```

Confirmed signals hardcoded in `decode.py`:

| Signal | CAN ID | Bytes | Formula |
|---|---|---|---|
| RPM      | 0x124 | [1:3] BE | / 4  (valid when byte0 == 0x08) |
| Throttle | 0x122 | [4]      | × 100/255 % |
| Brake    | 0x1AF | [6]      | bit 6 (`& 0x40`) |
| Speed    | 0x1B8 | [0:8]    | four 16-bit BE wheels / 32, averaged |

---

## Tune event thresholds (driving_mode.py)

Replay decoded CSVs through the exact detector logic before flashing:

```bash
cd can_sniffer/pc
python driving_mode.py ../../sound_sim/timelines/04_accel_to_second_gear_then_liftoff.csv
python driving_mode.py ../../sound_sim/timelines/*.csv --speed 2.0
python driving_mode.py ../../sound_sim/timelines/03_hard_pull_with_shifts.csv --plot
```

**Keyboard controls while replaying:**

| Key | Action |
|---|---|
| `1`–`4` | Toggle event 1/2/3/4 on/off |
| `m` | Toggle master enable |
| `+` / `-` | Volume ±0.1 |
| `r` | Hot-reload `shared/events_config.json` thresholds |
| `p` | Pause / resume |
| `q` | Quit |

Edit `can_sniffer/shared/events_config.json` and press `r` to apply thresholds
without restarting. Key thresholds to tune:

```jsonc
"e1_rpm_min":      4500,  // raise if E1 fires too easily
"e2_throttle_lo":    20,  // range of "was pressing" for lift-off
"e2_throttle_hi":    60,
"e3_sustain_ms":    300,  // ms of closed throttle before decel burble starts
"e4_drpm_dt":       -80,  // RPM/s drop rate for upshift detection
```

---

## Re-render pop sounds

After changing `sound_sim/config.json` (sound design tweaks):

```bash
cd sound_sim
python export.py --fmt raw --rate 22050 --out ../can_sniffer/pops/
```

Then re-upload pops to the device (see Firmware flash section below).

To A/B sound presets in the keyboard simulator:

```bash
cd sound_sim
python simulator.py   # keys 1/2/3/4 play events; r=reload; l=lock seed
```

Change `"active_preset"` in `sound_sim/config.json` to `subtle`, `m2_brapp`,
or `obnoxious`, then press `r`.

---

## Firmware flash (sound system mode)

```bash
# 1. Upload detector (shared between PC and firmware)
mpremote cp can_sniffer/shared/detector.py        :/detector.py

# 2. Upload firmware files
mpremote cp can_sniffer/firmware/mcp_driver.py    :/mcp_driver.py
mpremote cp can_sniffer/firmware/config.py         :/config.py
mpremote cp can_sniffer/firmware/sound_engine.py   :/sound_engine.py
mpremote cp can_sniffer/firmware/main_soundsystem.py :/main.py

# 3. Create /pops and upload all pop primitives (~800 KB)
mpremote mkdir /pops
for f in can_sniffer/pops/*.pcm can_sniffer/pops/manifest.json; do
    mpremote cp "$f" :/pops/
done

# 4. Upload events_config.json so the firmware can load thresholds
mpremote mkdir /shared
mpremote cp can_sniffer/shared/events_config.json :/shared/events_config.json

# 5. Soft-reset to start
mpremote reset
```

---

## Serial runtime commands

Connect with `mpremote` or any serial terminal (115200 baud) and type:

| Command | Effect |
|---|---|
| `E1 on` / `E1 off` | Enable/disable Anti-Lag Snap |
| `E2 on` / `E2 off` | Enable/disable Lift-Off Burble |
| `E3 on` / `E3 off` | Enable/disable Decel Burble |
| `E4 on` / `E4 off` | Enable/disable Soft Upshift |
| `MASTER on` / `MASTER off` | Global mute |
| `VOL 0.7` | Set master volume (0.0–1.0) |
| `STATUS` | Print current state |

Settings persist across reboots in `/runtime.json` on the device flash.

---

## File layout

```
can_sniffer/
  shared/
    detector.py           ← EventDetector (identical on PC + firmware)
    events_config.json    ← Thresholds + per-event toggles + volume
  pc/
    decode.py             ← SLCAN log → CSV (confirmed signals hardcoded)
    driving_mode.py       ← CSV replay + keyboard toggles + optional plot
    throttle_probe.py     ← Live signal dashboard while pressing pedal
    capture.py            ← Serial/UDP log recorder
  firmware/
    main_soundsystem.py   ← Upload as /main.py — the real in-car program
    sound_engine.py       ← I2S streaming from /pops/
    mcp_driver.py         ← MCP2515 SPI driver (listen-only)
    sniffer.py            ← Keep for re-sniffing sessions
    config.py             ← Pin assignments + CNF values
  pops/                   ← int16 mono .pcm at 22050 Hz (upload to device)
  firmware/logs/          ← Raw SLCAN captures from the car
sound_sim/
  make_pop.py             ← Synthesis primitives (tone + saturation + filter)
  events.py               ← Randomised event builder (build_event)
  config.json             ← Sound presets: subtle / m2_brapp / obnoxious
  simulator.py            ← Desktop A/B tool (keys 1/2/3/4)
  export.py               ← Render → can_sniffer/pops/
  timelines/              ← Decoded CSVs for driving_mode.py
```

---

## Failsafes

- **No CAN for > 2 s:** firmware mutes automatically; resumes when frames return
- **Boot grace period:** first 2 s after power-on are ignored (ECU wake-up noise)
- **CAN is fully passive:** MCP2515 in listen-only mode — no ACK, no error frames,
  zero risk to vehicle systems
- **Legal (DE):** cabin audio playback only; no external emission; CAN read-only

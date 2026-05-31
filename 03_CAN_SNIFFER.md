# CAN Sniffer — Capture & Reverse-Engineer Swift Sport Signals

> Companion to `01_PROJECT_OVERVIEW.md`. **Platform: ESP32 + MCP2515 module (SPI) running stock MicroPython.** No custom CAN firmware fork needed — CAN runs over standard SPI via the MCP2515 chip and its onboard TJA1050 transceiver.
>
> **Safety first:** ALWAYS run in listen-only mode while sniffing. In this mode the MCP2515 never drives the bus — no ACK, no error frames. Fully passive, cannot disturb the car.

---

## Hardware

| Part | Role | Notes |
|---|---|---|
| **ESP32** (DevKitC etc.) | Main controller + logger | WiFi for optional wireless logging |
| **MCP2515 module** (MCP2515 + TJA1050) | SPI CAN controller + transceiver | **8 MHz crystal.** Remove onboard 120Ω R1 — see below. |
| CAN tap | white=CAN-H, red=CAN-L | Already tapped at footwell. Keep stub short, twist leads. |
| USB | Log transport to PC | Most reliable for first bring-up; no WiFi setup needed |

**Fallback:** Pico 2 + same MCP2515 module over SPI runs the identical MicroPython firmware.

### Wiring: ESP32 ↔ MCP2515 module

| MCP2515 module pin | ESP32 pin | Notes |
|---|---|---|
| VCC | 5V (VIN) | Module is 5V logic |
| GND | GND | |
| CS | GPIO 5 | Chip select, active-low |
| SO / MISO | GPIO 19 | 5V output from module — see level note |
| SI / MOSI | GPIO 23 | |
| SCK | GPIO 18 | |
| INT | GPIO 4 | RX-ready interrupt (polled in firmware) |
| CANH | white (tap) | |
| CANL | red (tap) | |

### Crystal — 8 MHz (critical)

The MCP2515 has no auto-detect. Its bitrate registers are calculated from a hardcoded crystal frequency. A wrong crystal constant causes every frame to decode as garbage, or nothing at all.

- This module has an **8.000 MHz crystal**. `config.py` hardcodes `CAN_CRYSTAL = 8_000_000`. Never change this unless you swap the crystal.
- The firmware uses precomputed CNF register values for 8 MHz / 500 kbps (see `config.py`).
- **Wrong crystal constant is the #1 cause of "no frames / garbage frames"** — check this first.

### Termination — remove R1

The car bus already has its two 120Ω terminators. A third causes reflections and corrupted frames.

1. With engine off and ignition off, measure CANH–CANL with a DMM: expect **~60Ω** (two 120Ω in parallel).
2. The MCP2515 module has an onboard 120Ω resistor marked **R1** (sometimes a 0Ω jumper). Desolder it.
3. Re-measure after removal: still ~60Ω confirms the bus is intact and no third terminator is present.

### 3.3V / 5V level note

The MCP2515 module is 5V; ESP32 I/O is 3.3V.

- **MISO (SO) is the risk pin:** the module drives it to 5V, which exceeds the ESP32's rated input voltage.
- **For temporary sniffing:** direct connection usually works — most DevKitC boards tolerate 5V briefly on input-only pins — but it is not within spec.
- **For permanent install:** add a level shifter on the SPI lines (e.g. TXS0108E bidirectional 8-channel).
- If you see SPI init failures or corrupted reads, check the level mismatch before anything else.

---

## Firmware (MicroPython)

Standard MicroPython for ESP32 — no CAN-capable fork needed.

### Install

1. Flash stock MicroPython to ESP32: `https://micropython.org/download/ESP32_GENERIC/`
2. Copy the three firmware files to the ESP32 root using `mpremote cp` or Thonny:
   - `mcp_driver.py` — minimal MCP2515 SPI driver (reset, listen-only init, receive)
   - `config.py` — SPI pin assignments, crystal constant, CNF register values
   - `sniffer.py` — main firmware: Modes A / B / C, MARK injection
3. Run: `mpremote run sniffer.py` — or rename `sniffer.py` to `main.py` for auto-start on boot.

### Smoke-test

Run this first to confirm SPI wiring and chip communication before touching CAN. Paste into the MicroPython REPL or run with `mpremote run smoke_test.py`.

```python
# smoke_test.py
from machine import SPI, Pin
import time

spi = SPI(1, baudrate=1_000_000, polarity=0, phase=0,
          sck=Pin(18), mosi=Pin(23), miso=Pin(19))
cs = Pin(5, Pin.OUT, value=1)

def rd(reg):
    cs(0); spi.write(bytes([0x03, reg])); v = spi.read(1)[0]; cs(1); return v

def wr(reg, val):
    cs(0); spi.write(bytes([0x02, reg, val])); cs(1)

# Reset
cs(0); spi.write(bytes([0xC0])); cs(1)
time.sleep_ms(10)

stat = rd(0x0E)   # CANSTAT
ctrl = rd(0x0F)   # CANCTRL
print(f"CANSTAT=0x{stat:02X}  CANCTRL=0x{ctrl:02X}")

if (stat & 0xE0) == 0x80:
    print("OK — MCP2515 in config mode, SPI working")
else:
    print("FAIL — unexpected value; check wiring (swap MISO/MOSI if 0x00 or 0xFF)")
```

Expected: `CANSTAT=0x80  CANCTRL=0x87` — upper nibble 0x8_ means config mode.

### Firmware modes

Switch at runtime by sending a single character over serial (no newline needed):

| Char | Mode | Output format |
|---|---|---|
| `A` | Raw dump | `<millis> <hex_id> <dlc> <b0> <b1> …\r\n` |
| `B` | SLCAN ASCII | `t<ID3><DLC><DATA>\r` — import directly into SavvyCAN or python-can |
| `C` | candump `-tz -L` | `(<ts>) can0 <ID>#<DATA>\r\n` + `# MARKER` lines |

**Marker injection (Mode C):** send `MARK<label>` then Enter over serial REPL → inserts `# MARKER <label>` in the log so you can annotate "lift now", "shift now" while driving.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Smoke-test prints `0x00` / `0xFF` | MISO/MOSI swapped, or CS not toggling | Swap SO↔SI wires; check CS GPIO and polarity |
| Smoke-test CANSTAT ≠ 0x80 | SPI clock/phase mismatch | Try `polarity=1, phase=1` in SPI init |
| Smoke-test passes, zero CAN frames ever | Wrong baud or wrong crystal constant | Confirm crystal is 8 MHz; also try 250K then 125K |
| Garbage frames / CRC errors | Wrong crystal value, or 3rd 120Ω terminator on bus | Verify crystal; remove R1 from module |
| Intermittent frame loss after a while | 5V MISO damaging ESP32 input | Add TXS0108E level shifter |
| Frames arrive, IDs make no sense | CANH/CANL swapped | Swap white↔red tap wires |
| `init_listen_only()` returns `False` | MCP2515 can't confirm listen-only mode | Ensure `reset()` called first with 10 ms delay |

---

## Capture Session Plan

Run these as separate labeled logs. Quiet road, early morning. Use `MARK<label>` before each action.

| # | Scenario | What it isolates |
|---|---|---|
| 0 | Ignition ON, engine OFF, 30s | Baseline broadcast IDs (how rich is the bus?) |
| 1 | Idle, engine on, 30s | RPM idle value — which ID wobbles ~750 |
| 2 | Steady cruise ~50 km/h, 30s | Stable RPM + throttle + speed — easy to correlate |
| 3 | Gentle accel then gentle lift ×5 | Event 2 signature (throttle 20–60% → 0) |
| 4 | Full-load pull then hard lift ×3 | Event 1 signature (throttle >60% → 0, high RPM) |
| 5 | Long overrun coasting 10s | Event 3 signature (throttle 0, falling RPM, low load) |
| 6 | Normal city shifts <3000 rpm | Event 4 signature |
| 7 | Pedal sweep, stationary, in N | **Best capture for throttle/APP** — one variable, everything else still |
| 8 | Steering sweep / lights / etc. | Identify non-target IDs to rule out |

**Tip:** scenario 7 (slow stationary pedal 0→100→0) is the single most useful capture for finding the throttle/APP signal.

---

## Decode Workflow

### With SavvyCAN (recommended)
1. Import SLCAN log (Mode B output): File → Import
2. **Frame overview** → list all IDs by frequency. Powertrain RPM/APP typically 50–100 Hz.
3. Select a candidate ID, graph bytes over time, scrub to where you moved the pedal (use markers)
4. Identify byte/word that tracks the action monotonically
5. Determine width (8 vs 16-bit), endianness, scale/offset
6. Validate: RPM ~750 idle, rises smoothly; throttle 0–100%; speed matches GPS

### Known starting points
- **OpenDBC (comma.ai):** `github.com/commaai/opendbc` — search `suzuki/` for K14C-based DBCs. Swift / Vitara / Baleno share many signal IDs. May give you RPM + throttle straight away.
- **OBD2 PIDs** (if polling instead of sniffing broadcast): RPM `0x0C` = `(A*256+B)/4`, throttle `0x11` = `A*100/255`, speed `0x0D` = `A`.
- **`find_signal.py --diff`:** shows every ID with varying bytes at a glance. Start here after each capture.

### Manual fallback
Run `python find_signal.py logs/7_pedal_sweep.log`, pick a high-Hz ID, watch byte values while scrubbing — the throttle byte tracks the pedal.

---

## Output Artifacts

1. **`swift_signals.md`** — discovered signal table: `signal | CAN ID | byte | len | endian | scale | offset | observed range`
2. **`swift.dbc`** (optional) — emit with `python decode.py --emit-dbc swift.dbc`
3. **Decoded CSV timelines** — `python decode.py <log> --signals swift_signals.json` → CSVs in `sound_sim/timelines/` ready for `driving_mode.py`

---

## Suggested File Layout

```
can_sniffer/
  firmware/
    mcp_driver.py    # minimal MCP2515 SPI driver (listen-only, recv)
    config.py        # SPI pins, crystal constant, CNF registers, mode
    sniffer.py       # main firmware: Modes A/B/C, MARK injection
  pc/
    capture.py       # serial → labeled log files
    decode.py        # log + swift_signals.json → CSV timelines
    find_signal.py   # interactive byte-tracker
  logs/              # captured sessions (one file per scenario)
  swift_signals.json # signal table template (fill after capture session)
  swift_signals.md   # human-readable signal map + capture checklist
  swift.dbc          # optional — emitted by decode.py --emit-dbc
```

---

## Build Order

1. Wire ESP32 ↔ MCP2515, copy the three `.py` files, run `smoke_test.py`
2. Mode A — confirm frames arrive at 500K; if not, try 250K / 125K
3. `capture.py` to save first session log
4. `find_signal.py` on scenario 7 (pedal sweep) — find throttle byte
5. Mode C + MARK — run full capture session (scenarios 0–8)
6. Decode with SavvyCAN + cross-check against OpenDBC suzuki
7. Fill in `swift_signals.json`, run `decode.py` → CSVs into `sound_sim/timelines/`
8. Feed CSVs to `driving_mode.py` to validate and tune event detector thresholds

---

## Safety / Do-Not

- **Listen-only ALWAYS** while sniffing. `CANCTRL REQOP = 0b011`. The controller sends no ACK, no error frames. A misconfigured baud in normal mode can spam error frames and disturb the bus.
- **No third terminator.** Remove R1 from the module. Bus already has two 120Ω terminators.
- **Short stub, twisted tap leads** — preserve CAN signal integrity.
- **Never write CAN frames** in this project. Reading only.
- Common ground between ESP32 power source and car tap (chassis ground — verify if powering from a separate supply).
- Validate everything in listen-only before considering the tap permanent — only resolder the Stromdiebe to proper joints once you have confirmed clean frames.

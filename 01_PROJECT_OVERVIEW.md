# DIY Active Sound System — Suzuki Swift Sport ZC33S (2022, MHEV)

> **Master project document.** Two companion docs:
> - `02_SOUND_SIMULATOR.md` — desktop tool for designing/tweaking pop sounds
> - `03_CAN_SNIFFER.md` — firmware + workflow to capture CAN data from the car

---

## Project Goal

Cabin-only active sound system that adds burble / crackle / pop effects to a Suzuki Swift Sport ZC33S MHEV. Real driving events are detected via **raw CAN-bus access** (CAN already tapped, see below). Pop sounds are procedurally generated / sample-based, played through a **3D-printed exhaust-pipe resonator** driven by an 8" subwoofer.

**Design principles:**
- Read-only on CAN bus (zero risk to vehicle systems)
- Reversible / togglable
- No permanent vehicle modification required for the audio side

---

## Current Status

- ✅ CAN bus physically tapped at main harness (floor area, driver seat junction)
- ✅ CAN wires identified: **white = CAN-H, red = CAN-L** (verify by measurement: CAN-H sits slightly higher than CAN-L, both ~2.5V idle)
- ✅ Audio chain hardware decided (amp, sub, DAC)
- ✅ 3D printer available (Bambu H2D)
- ✅ Sound POC exists on PC (procedural synth, keys 1/2/3)
- ⬜ CAN sniffing / signal reverse-engineering — NEXT STEP
- ⬜ Port sound engine to MCU
- ⬜ Print + test exhaust-pipe enclosure

**Critical path:** sniff CAN → identify signal IDs → build event detector against real data → port sound engine → integrate.

---

## Microcontroller Decision: ESP32 (primary)

Switched from Pico 2 to **ESP32** — better fit for this project.

| Feature | ESP32 | Pico 2 |
|---|---|---|
| CAN controller | Built-in TWAI (optional for final build) or **MCP2515 over SPI** | MCP2515 over SPI |
| Wireless | WiFi + BT onboard | None |
| Clock | 240 MHz dual-core | 150 MHz dual-core |
| RAM | 520 KB | 264 KB |
| Audio I2S | Yes | Yes |

**CAN hardware chosen: MCP2515 module (MCP2515 + TJA1050, 8 MHz crystal) over SPI**, running MicroPython. No custom CAN firmware fork needed. The ESP32's built-in TWAI peripheral is an option for the final audio firmware, but MCP2515 SPI was chosen for sniffing because:
- MicroPython has no TWAI support
- The MCP2515 module is already on hand
- The same firmware runs unchanged on a Pico 2 fallback

**Wireless wins for this project:**
- Stream CAN logs over WiFi during sniffing (no USB tether in the footwell)
- Tweak sound params live from phone/laptop
- Toggle burble on/off via phone app or BLE
- OTA firmware updates

**Caveat:** I2S audio + WiFi can contend for timing. Pin audio to one core, WiFi/CAN to the other. Keep the audio DMA buffer healthy.

**Fallback:** Pico 2 + same MCP2515 module still valid if ESP32 audio/WiFi contention proves troublesome. Identical MicroPython sniffer firmware runs on either board.

---

## Hardware Stack

| Component | Part | Notes |
|---|---|---|
| MCU | **ESP32** (DevKitC or similar) | WiFi/BT. CAN via MCP2515 SPI (MicroPython). Fallback: Pico 2 + same module. |
| CAN controller | **MCP2515 module** (MCP2515 + TJA1050, 8 MHz crystal) | SPI-based, MicroPython. **Remove onboard 120Ω R1** — bus already terminated. Module is 5V; direct SPI works for sniffing; add level shifter for permanent install. |
| CAN tap | White=CAN-H, Red=CAN-L at footwell harness | Currently Stromdiebe (vampire taps). Resolder properly once validated. Keep stub < 30 cm, twist the tap leads. |
| DAC | PCM5102A | I2S input, analog out → amp |
| Amplifier | Clyxgs TPA3116D2 (2×50W) | Class-D, 12V direct. Analog in from DAC. |
| Speaker | **DY200-9A** (8", 100W, 8Ω, 90 dB/W/m, 40Hz–5kHz) | Already owned. ~25–30W usable at 12V/8Ω. Plenty. |
| Power | 12V from car → buck to 5V/3.3V | ESP32 + DAC. Amp on 12V direct. |
| Toggle | TBD | BLE app (ESP32 makes this trivial) or physical switch |

---

## CAN Bus Notes

- **Bus tapped at footwell harness** — likely powertrain CAN (full broadcast traffic), much richer than gateway-filtered OBD2 port. This is the good position.
- **Expected baud:** 500 kbps (powertrain). Confirm with sniffer — if frames don't decode cleanly, try 250 / 125 kbps.
- **Listen-only mode mandatory** during all sniffing (MCP2515: `CANCTRL REQOP = 0b011`). Chip sends no ACK/error frames — fully passive, cannot disturb the car.
- **Termination:** bus already has its two 120Ω terminators. Your transceiver board must NOT add a third — remove/disable its onboard resistor.

### Signals We Want (to reverse-engineer)

| Signal | Why | Expected update rate |
|---|---|---|
| Engine RPM | Core trigger for all events | 100 Hz+ |
| Accelerator pedal (APP) / Throttle (TPS) | Lift detection — the key signal | 50–100 Hz |
| Vehicle speed | Distinguish cruise/decel/idle | 50 Hz |
| Engine load | Distinguish load states | medium |
| Gear position | Better shift detection | event-based |
| Clutch switch | Clean shift detection | event-based |
| Brake switch | Suppress burble under braking | event-based |
| ISG status (MHEV) | Regen alters decel character | medium |

See `03_CAN_SNIFFER.md` for the capture + decode workflow.

---

## Target Sound Events (4 total)

Detection uses rolling history buffers (last 5–10 readings) and derivatives — not raw values.

### Event 1 — Anti-Lag / High-RPM Upshift Snap
**Trigger (full load + shift):**
- RPM was > 4500
- Throttle was > 60%
- Throttle drops to < 5% very fast (>90% drop in <150ms)
- Engine load was > 70%
- Optional: RPM recovers within 500ms

**Sound:** 4–6 rapid overlapping pops, 60–180 Hz, high noise mix (0.6–0.8) → aggressive CRACK

### Event 2 — Lift-Off Burble
**Trigger (gentle throttle lift, normal driving):**
- RPM 2500–4500
- Throttle before lift: 20–60% (not full load)
- Throttle drops moderately (50→0% in 200–400ms)
- Engine load was < 60%
- Speed > 30 km/h
- NOT while Event 1 active

**Sound:** 3–5 soft pops over 400–600ms, 90–160 Hz, noise ratio 0.4–0.6, slight volume taper → "buh-buh"

### Event 3 — Decel Burble (long)
**Trigger (sustained overrun):**
- Throttle < 5%
- RPM > 2000
- Speed > 20 km/h
- Engine load < 15%
- Sustained ≥ 300ms
- Chains after Event 2 (800–1500ms pause)

**Sound:** 10–15 irregular pops over 1.5s, 80–150 Hz, volume tapers, randomized gaps → rolling burble

### Event 4 — Soft Upshift < 3000 RPM
**Trigger:**
- RPM drop signature
- RPM was below 3000 before drop

**Sound:** 1–2 soft pops, 120–260 Hz, noise ratio 0.3–0.4, shorter decay → gentle tick

### Event Coordination
- Mutex per event type
- Cooldowns: E1 1000ms, E2 400ms, E3 2000ms, E4 600ms
- Priority: E1 blocks E2/E4
- Chain: E2 → 800–1500ms pause → E3 if conditions persist

---

## Software Architecture (ESP32)

```
┌────────────────────────────┐   ┌────────────────────────────┐
│ Core 0                      │   │ Core 1                      │
│                             │   │                             │
│ TWAI CAN reader (listen)    │   │ Audio synthesis / mixer     │
│   → parse signal frames     │   │   → fills I2S DMA buffer    │
│   → history ring buffer     │   │   → PCM5102A                │
│         ↓                   │   │         ▲                   │
│ Event detector (state mc)   │──▶│ Sound engine (voice pool)   │
│   → emits event flags       │   │   triggered by event flags  │
│                             │   │                             │
│ (WiFi/BLE control optional) │   │                             │
└────────────────────────────┘   └────────────────────────────┘
        FreeRTOS queue (event flags + params)
```

- **Core 0:** CAN + detection + connectivity
- **Core 1:** audio only (keep it real-time clean, isolate from WiFi jitter)
- **Audio:** I2S DMA to PCM5102A. Pre-rendered pop primitives in flash + runtime randomization (pitch/gain/spacing). See `02_SOUND_SIMULATOR.md` for sound design that feeds this.

---

## 3D-Printed Exhaust-Pipe Resonator

Speaker-driven resonance pipe imitating exhaust acoustics: standing-wave pipe resonance + flared mouth radiation.

### Design (scaled for 8" DY200-9A)
```
Speaker Chamber          Resonance Pipe              Exit Cone
┌──────────────────┐                              
│  DY200-9A 8"     │═══════════════════════════   ╱──────╲
│  (200 mm Ø)      │═══════════════════════════            
│                  │                              ╲──────╱
└──────────────────┘                              
  8–12 L sealed,        140 mm Ø, 40–50 cm         140 → 180 mm
  wool-stuffed          constant section           flare, 12–15 cm
```

- **Chamber:** 8–12 L sealed, 60–80% poly-fill. Sealed (not ported) for clean transient punch.
- **Pipe:** 140 mm Ø, 40–50 cm. Fundamental ~190 Hz, 2nd harmonic ~95 Hz lands in pop spectrum → free resonant boost.
- **Cone:** 140→180 mm over 12–15 cm, smooth curve → megaphone effect.

### Print (Bambu H2D)
- Material: **ASA** (heated chamber, heat-resistant for car) or ABS
- Wall: 5 mm, ≥5 perimeters (airtight + no panel resonance)
- TPU gaskets co-printed (dual-nozzle) for speaker seal + pipe joints
- Pipe in 1–2 segments (320 mm bed limit), bayonet/threaded joints
- Modular interchangeable exit cones for tuning

### Mounting
Trunk floor, longitudinal, muzzle rearward — authentic sound direction, protected, space available.

### Tuning sequence
1. Straight pipe + simple cone, bench test vs sound POC
2. Hand-over-muzzle test to hear resonance shift
3. Swap 3–4 cone variants
4. Optional: internal ribs / Helmholtz bulge if too "tubey"
5. Match sound-engine pop frequencies to measured pipe response

---

## Audio Sample-Capture Side Project (separate from CAN)

Parallel experiment: live mic-to-speaker exhaust amplification (4 lavalier mics → mixer → AUX). Mostly a source for recording real pop samples to feed the synthesizer. Status: testing mic/adapter chain (TRRS Boya mics need TRRS→TRS adapters; mixer is power-only USB, audio via 3.5mm master-out → car AUX which is TRS). Not on the critical path; keep separate from CAN/sound-engine work.

---

## Legal (DE, Baden-Württemberg)
- Read-only CAN: legal, no cert impact
- Cabin audio playback: like playing music — legal
- Keep cabin volume reasonable (§30 StVO)
- Do NOT route audio outside vehicle (§49 StVZO)
- Do NOT write CAN frames

---

## Master To-Do

### CAN / data (see 03)
- [ ] Build sniffer firmware on ESP32 + MCP2515 (SPI, MicroPython)
- [ ] Verify white=CAN-H / red=CAN-L by measurement
- [ ] Confirm baud (500 first)
- [ ] Capture: idle, cruise, gentle lift, hard lift, long decel, soft shift
- [ ] Decode RPM, APP/TPS, speed, load IDs (SavvyCAN / OpenDBC suzuki)

### Sound (see 02)
- [ ] Port existing PC POC into the simulator tool structure
- [ ] Add Event 2 (Lift-Off)
- [ ] Tune params, export pop-primitive library
- [ ] Port engine to ESP32 I2S

### Hardware / enclosure
- [ ] Bench-test audio chain (ESP32 → DAC → TPA3116 → DY200-9A)
- [ ] Print chamber + first pipe + cones
- [ ] Resolder CAN tap properly once validated
- [ ] Toggle mechanism (BLE app)
- [ ] Failsafe: CAN timeout > 2s → mute

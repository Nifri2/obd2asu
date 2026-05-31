# Sound Simulator & Tweaking Tool — Burble Sound Design

> Companion to `01_PROJECT_OVERVIEW.md`. This is the **desktop tool** for designing, auditioning, and exporting the pop/burble sounds that later run on the ESP32. Goal: iterate on sound character fast on a PC before committing to firmware.

> **Note:** an existing procedural POC (`burble_poc.py`, keyboard keys 1/2/3) already lives on the user's PC. This tool extends it. Ask the user to share that file; reuse `make_pop()` rather than rewriting from scratch.

---

## Purpose

1. **Audition** the 4 event sounds on demand (keyboard or GUI buttons)
2. **Tweak** synthesis parameters live (frequency ranges, pop counts, noise ratio, decay, timing) without editing code each time
3. **Simulate** a driving sequence (e.g. "accelerate → lift → decel") to hear events fire in realistic succession
4. **Export** a library of pre-rendered pop primitives as raw int16 PCM for embedding in ESP32 firmware

---

## Core Synthesis Primitive

Carried over from the POC. The fundamental building block:

```python
def make_pop(freq, duration_ms, noise_ratio, volume, sample_rate=22050):
    """
    One exhaust 'pop':
      - sine tone at `freq`
      - white noise burst
      - mixed per `noise_ratio` (0=pure tone, 1=pure noise)
      - exponential decay envelope (fast attack, slow tail)
      - scaled by `volume`
    Returns float32 numpy array in [-1, 1].
    """
    # 1. t = arange(samples) / sample_rate
    # 2. tone = sin(2*pi*freq*t)
    # 3. noise = uniform(-1, 1, samples)
    # 4. mix = (1-noise_ratio)*tone + noise_ratio*noise
    # 5. env = exp(-t * decay_rate)   # decay_rate tuned for ~duration_ms
    # 6. return mix * env * volume
```

Keep the desktop version numpy-based for speed/quality. The ESP32 port replaces numpy with fixed-point / `array` math (see Export section).

---

## The 4 Event Sounds (parameter targets)

These mirror `01_PROJECT_OVERVIEW.md`. The tool should expose every number below as a live-adjustable parameter with sensible min/max.

| Event | Freq Hz | n Pops | Duration | Noise ratio | Volume | Gap ms | Character |
|---|---|---|---|---|---|---|---|
| 1 Anti-Lag Snap | 60–180 | 4–6 | overlapping | 0.6–0.8 | 0.8–1.0 | 30–80 | aggressive CRACK |
| 2 Lift-Off Burble | 90–160 | 3–5 | 400–600ms | 0.4–0.6 | 0.5–0.75 | 60–140 | soft "buh-buh" |
| 3 Decel Burble | 80–150 | 10–15 | ~1.5s | 0.5–0.7 | 0.6–0.8 | 80–200 | rolling burble |
| 4 Soft Upshift | 120–260 | 1–2 | 100–200ms | 0.3–0.4 | 0.4–0.6 | n/a | gentle tick |

**Randomization (essential):** every trigger randomizes within the ranges — pop count, per-pop frequency, gap timing, volume, noise ratio. Prevents the ear detecting a loop. Each event builds a sequence by scheduling N `make_pop()` outputs at randomized offsets, then mixing/overlapping them onto one buffer.

```python
def build_event(event_cfg):
    """
    event_cfg holds the ranges from the table above.
    1. pick n_pops in range
    2. for each pop: random freq/dur/noise/vol within ranges
    3. schedule at cumulative randomized gap offsets
    4. apply per-sequence volume taper (esp. event 3)
    5. mix all pops onto a single output buffer (allow overlap)
    """
```

---

## Tool Features

### Minimum (build this first)
- Keyboard triggers `1` `2` `3` `4` → play events 1–4 (extend POC's 1/2/3)
- Each press re-randomizes (audibly different every time)
- Plays through default audio out (`sounddevice`)

### Tweaking layer
- Live-editable parameters per event. Two acceptable implementations:
  - **Simple:** a `config.py` / JSON with all ranges; reload on a keypress (`r`)
  - **Nicer:** minimal GUI (e.g. `dearpygui` or `tkinter`) with sliders per parameter + a "Play" button per event
- Show the actual randomized values chosen for the last trigger (so the user can see "that good one was 112 Hz, 5 pops, 0.55 noise")
- A "lock seed" toggle to reproduce a specific good-sounding instance

### Driving simulator mode
- Replay a scripted timeline of driving states (RPM, throttle, speed, load over time) and run the **same event-detection logic** that will run on the ESP32, so events fire automatically
- Two timeline sources:
  1. Hand-written scripts (e.g. `cruise_then_lift.csv`)
  2. **Real captured CAN logs** decoded into signal timelines (output of `03_CAN_SNIFFER.md`)
- This validates detection thresholds against real data *and* lets the user hear how it'll actually sound while driving — before touching firmware
- Visual: simple matplotlib scrolling plot of RPM/throttle with event markers, optional

### Pipe-response option (nice-to-have)
- Optional convolution of output with an impulse response of the 3D-printed pipe (once recorded), to preview how sounds will color through the resonator
- Until an IR exists: a simple resonant bandpass around the pipe fundamental (~95–190 Hz) approximates it

---

## Event Detection Logic (shared with firmware)

Keep this logic **identical** to what runs on the ESP32 so the simulator is a true preview. Implement it once, cleanly, so it can be ported directly.

```python
class EventDetector:
    """
    Consumes a stream of (timestamp, rpm, throttle, speed, load) samples.
    Maintains rolling history (deque ~10 samples).
    Computes derivatives: dRPM/dt, dThrottle/dt.
    Emits at most one event per tick, respecting cooldowns + priority.
    """
    # Event 1 (Anti-Lag): rpm_was>4500, throttle_was>60, fast drop<5%, load_was>70
    # Event 2 (Lift-Off): rpm 2500-4500, throttle_was 20-60, moderate drop, load_was<60, speed>30, not E1
    # Event 3 (Decel):    throttle<5, rpm>2000, speed>20, load<15, sustained>=300ms, chains after E2
    # Event 4 (Soft):     rpm-drop signature, rpm_was<3000
    # Cooldowns: E1 1000, E2 400, E3 2000, E4 600 (ms)
    # Priority: E1 blocks E2/E4
```

Detection thresholds **will need retuning** against real captured data — the numbers above are estimates. The simulator's driving mode is exactly where that tuning happens.

---

## Export: Pop-Primitive Library for ESP32

The ESP32 won't synthesize from scratch at runtime (MicroPython/Arduino GC + float cost risks audio glitches). Instead:

1. Generate **5–10 pop primitives per event** offline using `make_pop()` with representative parameters
2. Export each as **raw int16 mono PCM** at the firmware sample rate (22050 Hz)
3. Emit a C header (`pops.h`) or binary blobs for flash storage
4. ESP32 runtime: select a random primitive, optionally resample for pitch variation, scale gain, schedule with randomized gaps → reconstructs the "never the same twice" behavior cheaply

```python
def export_library(out_dir, sample_rate=22050, fmt="c_header"):
    """
    For each event, render a handful of primitives, normalize, convert to int16.
    fmt='c_header' -> pops.h with const int16_t arrays + lengths
    fmt='raw'      -> .pcm files + manifest.json
    Keep total under flash budget (~4MB easily fits 30+ short pops).
    """
```

Budget reminder: 22050 Hz × 16-bit mono ≈ 43 KB/sec. A 150ms pop ≈ 6.5 KB. 40 pops ≈ 260 KB. Trivial.

---

## Dependencies
- `numpy` — synthesis
- `sounddevice` — playback
- `pynput` or GUI lib — triggers
- `matplotlib` (optional) — driving-mode visualization
- Standard lib: `json`, `random`, `dataclasses`

---

## Suggested File Layout
```
sound_sim/
  make_pop.py          # core primitive (from POC)
  events.py            # build_event() + event configs
  detector.py          # EventDetector (PORT THIS TO FIRMWARE verbatim-ish)
  config.json          # all tunable ranges
  simulator.py         # keyboard/GUI entry point
  driving_mode.py      # timeline replay + detector + auto-trigger
  export.py            # pop-primitive library exporter
  timelines/           # scripted + captured-CAN-derived driving scripts
  out/                 # exported pops.h / .pcm
```

---

## Build Order
1. Drop in existing `make_pop()` from user's POC
2. `events.py` with the 4 configs + randomization → keyboard triggers (extend 1/2/3 to add 4)
3. `config.json` + hot-reload so tuning needs no code edits
4. `detector.py` — clean, portable
5. `driving_mode.py` — feed scripted timelines, then real CAN logs once captured
6. `export.py` — primitive library for firmware
7. (optional) GUI + pipe-IR convolution

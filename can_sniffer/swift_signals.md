# Swift Sport ZC33S — CAN Signal Map

> Fill this in after the capture session. Update `swift_signals.json` in parallel so `decode.py` can use it directly.

## Discovered Signals

| Signal | CAN ID | Byte offset | Length | Endian | Scale | Offset | Unit | Observed range | Notes |
|---|---|---|---|---|---|---|---|---|---|
| RPM | ? | ? | 2 | big | 0.25 | 0 | rpm | ~750 idle, ~6000 red | |
| Throttle / APP | ? | ? | 1 | big | 0.392 | 0 | % | 0–100 | Pedal sweep isolates cleanly |
| Vehicle speed | ? | ? | 2 | big | 0.01 | 0 | km/h | 0–180 | Match to speedometer |
| Engine load | ? | ? | 1 | big | 0.392 | 0 | % | 0–100 | Low at cruise, high under load |
| Gear position | ? | ? | 1 | big | 1.0 | 0 | gear | 1–6 | Optional |
| Clutch switch | ? | ? | 1 | big | 1.0 | 0 | bool | 0/1 | Optional |
| Brake switch | ? | ? | 1 | big | 1.0 | 0 | bool | 0/1 | Optional |
| ISG status | ? | ? | 1 | big | 1.0 | 0 | enum | 0/1/2 | Optional — MHEV regen state |

## OBD2 Standard PIDs (reference, if polling instead of sniffing broadcast)

These use a different ID scheme (functional request 0x7DF → ECU response 0x7E8/7E9).
The broadcast frames on the powertrain bus use **different, proprietary IDs** — the table above is what you're hunting.

| PID | Signal | Formula |
|---|---|---|
| 0x0C | RPM | (A\*256+B)/4 |
| 0x11 | Throttle position | A\*100/255 |
| 0x0D | Vehicle speed | A km/h |
| 0x04 | Calculated engine load | A\*100/255 |
| 0x49 | Accelerator pedal D | A\*100/255 |

## Known starting points

- **OpenDBC (comma.ai):** `github.com/commaai/opendbc` — search `suzuki/` for K14C-based DBCs. The Swift, Vitara, Baleno, and S-Cross share many signal IDs on this platform. May give you RPM + throttle straight away.
- **Signal ID frequency (from find_signal.py):** RPM and throttle are typically 50–100 Hz; speed is ~50 Hz. Run `python find_signal.py <log> --diff` to see all varying bytes at a glance.
- **Pedal sweep (scenario 7):** stationary, engine on, slowly press pedal 0→100→0 while logging. Only the throttle signal moves — makes it trivially easy to spot.

## Capture session log

| Scenario | Log file | Notes |
|---|---|---|
| 0 — ignition on, engine off | | |
| 1 — idle | | |
| 2 — steady cruise ~50 km/h | | |
| 3 — gentle accel + lift ×5 | | |
| 4 — full-load pull + hard lift ×3 | | |
| 5 — long overrun coasting | | |
| 6 — normal city shifts <3000 rpm | | |
| 7 — pedal sweep, stationary | | |
| 8 — steering / lights sweep | | |

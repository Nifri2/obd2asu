"""
throttle_probe.py — live RPM + throttle candidate dashboard.

Reads SLCAN frames from the ESP32 sniffer over USB serial and refreshes a
live display so you can watch which byte tracks the pedal.

Close any mpremote session first — only one program can hold the serial port.

Usage:
  python throttle_probe.py
  python throttle_probe.py --port /dev/ttyUSB0 --baud 115200
  python throttle_probe.py --record session.slcan
"""

import argparse
import sys
import time

import serial

# ── Signal definitions ────────────────────────────────────────────────────────

RPM_ID    = 0x124
RPM_VALID = 0x08   # data[0] must equal this for a valid RPM frame

# (label, can_id, min_dlc, decoder: bytes -> (raw: int, pct: float | None))
CANDIDATES = [
    ("0x13F 16bit", 0x13F, 3, lambda d: (int.from_bytes(d[1:3], "big"), None)),
    ("0x13F b1",    0x13F, 2, lambda d: (d[1], d[1] * 100 / 255)),
    ("0x13F b2",    0x13F, 3, lambda d: (d[2], d[2] * 100 / 255)),
    ("0x1AF b6",    0x1AF, 7, lambda d: (d[6], d[6] * 100 / 255)),
    ("0x1AF b7",    0x1AF, 8, lambda d: (d[7], d[7] * 100 / 255)),
    ("0x129 b2",    0x129, 3, lambda d: (d[2], d[2] * 100 / 255)),
]

# ── State ─────────────────────────────────────────────────────────────────────

rpm:         float | None = None
frame_count: int          = 0
fps:         float        = 0.0
_fps_count:  int          = 0
_t_fps:      float        = time.monotonic()

# per-candidate: {"raw": int|None, "pct": float|None, "min": int|None, "max": int|None}
cstate: dict[str, dict] = {
    label: {"raw": None, "pct": None, "min": None, "max": None}
    for label, *_ in CANDIDATES
}

# ── Parser ────────────────────────────────────────────────────────────────────

def parse_slcan(raw: bytes) -> tuple[int, bytes] | None:
    """Parse b't<ID3><DLC><DATA>' or b'T<ID8><DLC><DATA>' → (can_id, data) or None."""
    try:
        s = raw.decode("ascii").strip()
        if not s or s[0] not in ("t", "T"):
            return None
        if s[0] == "t":
            can_id = int(s[1:4], 16)
            dlc    = int(s[4])
            data   = bytes(int(s[5 + i*2 : 7 + i*2], 16) for i in range(dlc))
        else:
            can_id = int(s[1:9], 16)
            dlc    = int(s[9])
            data   = bytes(int(s[10 + i*2 : 12 + i*2], 16) for i in range(dlc))
        return (can_id, data)
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


def process(can_id: int, data: bytes) -> None:
    global rpm, frame_count, _fps_count

    frame_count += 1
    _fps_count  += 1

    if can_id == RPM_ID and len(data) >= 3 and data[0] == RPM_VALID:
        rpm = int.from_bytes(data[1:3], "big") / 4

    for label, cid, min_dlc, decoder in CANDIDATES:
        if can_id != cid or len(data) < min_dlc:
            continue
        try:
            raw, pct = decoder(data)
        except (IndexError, ValueError):
            continue
        s = cstate[label]
        s["raw"] = raw
        s["pct"] = pct
        if s["min"] is None or raw < s["min"]:
            s["min"] = raw
        if s["max"] is None or raw > s["max"]:
            s["max"] = raw

# ── Display ───────────────────────────────────────────────────────────────────

_CLEAR  = "\x1b[2J\x1b[H"
_HOME   = "\x1b[H"
_CLEOL  = "\x1b[K"   # clear to end of line


def _line(s: str = "") -> str:
    return s + _CLEOL + "\n"


def draw() -> None:
    out = [_HOME]

    rpm_s = f"{rpm:.0f}" if rpm is not None else "---"
    out.append(_line(f"  RPM: {rpm_s:<8}  frames: {frame_count:<8}  fps: {fps:.0f}"))
    out.append(_line())
    out.append(_line("  --- throttle candidates ---"))
    out.append(_line())

    for label, *_ in CANDIDATES:
        s = cstate[label]

        raw_s = f"{s['raw']:>5}"  if s["raw"] is not None else "  ---"
        pct_s = (f"{s['pct']:>4.0f}%" if s["pct"] is not None else "  n/a")

        if s["min"] is not None:
            mn, mx = s["min"], s["max"]
            span   = mx - mn
            stat_s = f"  [min {mn:>4}  max {mx:>4}  span {span:>4}]"
        else:
            stat_s = "  [no data]"

        out.append(_line(f"  {label:<14}  {raw_s} ({pct_s}){stat_s}"))

    out.append(_line())
    out.append(_line("  Ctrl+C to stop and print summary."))

    sys.stdout.write("".join(out))
    sys.stdout.flush()


def print_summary() -> None:
    print("\n\n=== Session summary ===\n")
    print(f"  {'candidate':<14}  {'min':>5}  {'max':>5}  {'span':>5}")
    print(f"  {'-'*14}  {'-'*5}  {'-'*5}  {'-'*5}")
    for label, *_ in CANDIDATES:
        s = cstate[label]
        if s["min"] is None:
            print(f"  {label:<14}  {'---':>5}  {'---':>5}  {'---':>5}  (no frames)")
        else:
            span = s["max"] - s["min"]
            print(f"  {label:<14}  {s['min']:>5}  {s['max']:>5}  {span:>5}")
    print()
    print("  Hint: the candidate with span closest to 0..255 (1-byte signals) that")
    print("        returns to near 0 when released is most likely the pedal (APP).")
    print("        0x13F 16bit may use a 0..~65535 scale — check its span separately.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global fps, _fps_count, _t_fps

    parser = argparse.ArgumentParser(description="Live RPM + throttle candidate dashboard")
    parser.add_argument("--port",   default="/dev/ttyUSB0", help="Serial port")
    parser.add_argument("--baud",   type=int, default=115200, help="Serial baud rate")
    parser.add_argument("--record", metavar="FILE",
                        help="Append raw SLCAN frames to FILE for offline analysis")
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0)
    except serial.SerialException as e:
        sys.exit(f"Cannot open {args.port}: {e}\n"
                 f"  Make sure mpremote / Thonny is disconnected and the sniffer is running as main.py")

    record_fh = open(args.record, "a") if args.record else None

    sys.stdout.write(_CLEAR)
    sys.stdout.flush()

    buf:         bytes = b""
    t_last_draw: float = time.monotonic()

    try:
        while True:
            chunk = ser.read(4096)

            if chunk:
                if record_fh:
                    record_fh.write(chunk.decode("ascii", errors="replace"))
                    record_fh.flush()
                buf += chunk

            # Process every complete \r-terminated frame in the buffer
            while b"\r" in buf:
                raw_line, buf = buf.split(b"\r", 1)
                raw_line = raw_line.lstrip(b"\n")   # discard leading \n from prior \r\n
                if not raw_line or raw_line.startswith(b"#"):
                    continue
                parsed = parse_slcan(raw_line)
                if parsed:
                    process(*parsed)

            now = time.monotonic()

            # FPS: update once per second
            elapsed = now - _t_fps
            if elapsed >= 1.0:
                fps        = _fps_count / elapsed
                _fps_count = 0
                _t_fps     = now

            # Refresh display at ~10 Hz
            if now - t_last_draw >= 0.1:
                draw()
                t_last_draw = now
            else:
                time.sleep(0.005)

    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        if record_fh:
            record_fh.close()

    print_summary()


if __name__ == "__main__":
    main()

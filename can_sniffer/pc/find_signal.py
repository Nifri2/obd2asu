"""
CAN signal finder — interactive byte-tracking helper.

Parses a captured log (Mode A or Mode C), shows which IDs are present and
how frequently, then lets you drill into a single ID to watch byte values
over time and spot which bytes track a physical signal.

Usage:
  python find_signal.py logs/7_pedal_sweep.log
  python find_signal.py logs/7_pedal_sweep.log --watch 0x1A0 3
  python find_signal.py logs/7_pedal_sweep.log --diff
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path


# ── Log parser ────────────────────────────────────────────────────────────────

def detect_format(first_line: str) -> str:
    """Return 'A', 'B', or 'C' based on the first non-comment data line."""
    if first_line.startswith("t") or first_line.startswith("T"):
        return "B"
    if first_line.startswith("("):
        return "C"
    return "A"


def parse_log(path: Path) -> list[dict]:
    """
    Parse log into list of {ts_ms, can_id, data: bytes, marker: str|None}.
    Handles Mode A, B (SLCAN), and C (candump).
    """
    frames = []
    fmt = None
    first_ts = None

    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("#"):
                # Capture marker
                if "MARKER" in line:
                    label = line.split("MARKER", 1)[1].strip()
                    frames.append({"ts_ms": None, "can_id": None, "data": None, "marker": label})
                continue

            if fmt is None:
                fmt = detect_format(line)

            try:
                if fmt == "A":
                    parts = line.split()
                    ts_ms = float(parts[0])
                    can_id = int(parts[1], 16)
                    dlc = int(parts[2])
                    data = bytes(int(x, 16) for x in parts[3:3 + dlc])

                elif fmt == "B":
                    if line[0] == "t":
                        can_id = int(line[1:4], 16)
                        dlc = int(line[4])
                        data = bytes(int(line[5 + i*2:7 + i*2], 16) for i in range(dlc))
                    else:  # T (extended)
                        can_id = int(line[1:9], 16)
                        dlc = int(line[9])
                        data = bytes(int(line[10 + i*2:12 + i*2], 16) for i in range(dlc))
                    ts_ms = len(frames) * 1.0  # SLCAN has no timestamp; use index

                elif fmt == "C":
                    ts_str = line[1:line.index(")")]
                    ts_s = float(ts_str)
                    rest = line[line.index(")") + 1:].strip()
                    parts = rest.split()
                    id_data = parts[1] if len(parts) > 1 else parts[0]
                    can_id_str, data_str = id_data.split("#", 1)
                    can_id = int(can_id_str, 16)
                    data = bytes.fromhex(data_str)
                    ts_ms = ts_s * 1000.0

                else:
                    continue

                if first_ts is None and fmt != "B":
                    first_ts = ts_ms
                if first_ts is not None and fmt != "B":
                    ts_ms -= first_ts

                frames.append({"ts_ms": ts_ms, "can_id": can_id, "data": data, "marker": None})

            except (ValueError, IndexError):
                continue

    return frames


# ── Analysis ──────────────────────────────────────────────────────────────────

def id_stats(frames: list[dict]) -> list[dict]:
    """Return list of {can_id, count, hz, dlc} sorted by count desc."""
    counts = defaultdict(int)
    dlcs   = defaultdict(set)
    first  = {}
    last   = {}
    for f in frames:
        if f["can_id"] is None:
            continue
        cid = f["can_id"]
        counts[cid] += 1
        if f["data"]:
            dlcs[cid].add(len(f["data"]))
        ts = f["ts_ms"]
        if ts is not None:
            if cid not in first:
                first[cid] = ts
            last[cid] = ts

    stats = []
    for cid, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        span_ms = (last.get(cid, 0) - first.get(cid, 0)) or 1
        hz = cnt / (span_ms / 1000.0) if span_ms > 10 else 0
        stats.append({"can_id": cid, "count": cnt, "hz": hz,
                      "dlc": max(dlcs[cid]) if dlcs[cid] else 0})
    return stats


def byte_diff_summary(frames: list[dict], can_id: int) -> dict[int, dict]:
    """
    For each byte position in frames for `can_id`, compute:
    min, max, n_unique values, n_changes.
    """
    target = [f for f in frames if f["can_id"] == can_id and f["data"]]
    if not target:
        return {}

    dlc = max(len(f["data"]) for f in target)
    summary = {}
    for b in range(dlc):
        vals = [f["data"][b] for f in target if len(f["data"]) > b]
        prev = None
        changes = 0
        for v in vals:
            if prev is not None and v != prev:
                changes += 1
            prev = v
        summary[b] = {
            "min": min(vals), "max": max(vals),
            "n_unique": len(set(vals)), "changes": changes,
        }
    return summary


# ── Display helpers ───────────────────────────────────────────────────────────

def print_id_table(stats: list[dict]) -> None:
    print(f"\n  {'#':>4}  {'CAN ID':>8}  {'Count':>7}  {'Hz':>7}  {'DLC':>4}")
    print("  " + "-" * 38)
    for i, s in enumerate(stats[:40]):
        print(f"  {i:4d}  0x{s['can_id']:03X}      {s['count']:>7d}  {s['hz']:>7.1f}  {s['dlc']:>4d}")


def watch_bytes(frames: list[dict], can_id: int, tail: int = 30) -> None:
    target = [f for f in frames if f["can_id"] == can_id and f["data"]]
    if not target:
        print(f"  No frames for ID 0x{can_id:X}")
        return

    dlc = max(len(f["data"]) for f in target)
    header = f"  {'ts_ms':>8}  " + "  ".join(f"B{b:d}" for b in range(dlc))
    print(header)
    print("  " + "-" * len(header))

    prev_data = None
    shown = target[-tail:]
    for f in shown:
        d = f["data"]
        cells = []
        for b in range(dlc):
            v = d[b] if b < len(d) else 0
            changed = prev_data is not None and b < len(prev_data) and v != prev_data[b]
            cells.append(f"\033[1;33m{v:3d}\033[0m" if changed else f"{v:3d}")
        ts = f"{f['ts_ms']:.1f}" if f["ts_ms"] is not None else "?"
        print(f"  {ts:>8}  " + "  ".join(cells))
        prev_data = d


def diff_mode(frames: list[dict], stats: list[dict]) -> None:
    print(f"\n  {'CAN ID':>8}  {'DLC':>4}  {'Byte':>5}  {'Min':>5}  {'Max':>5}  {'Uniq':>6}  {'Changes':>8}")
    print("  " + "-" * 55)
    for s in stats[:20]:
        cid = s["can_id"]
        bsummary = byte_diff_summary(frames, cid)
        for b, info in bsummary.items():
            if info["changes"] > 0:
                print(f"  0x{cid:03X}      {s['dlc']:4d}  {b:5d}  "
                      f"{info['min']:5d}  {info['max']:5d}  "
                      f"{info['n_unique']:6d}  {info['changes']:8d}")


# ── Entry points ──────────────────────────────────────────────────────────────

def interactive(frames: list[dict], stats: list[dict]) -> None:
    print_id_table(stats)
    print("\n  High Hz = likely powertrain (RPM, throttle, speed)")
    print("  Tip: scenario 7 (pedal sweep) makes throttle byte obvious\n")

    while True:
        try:
            raw = input("  Enter index or hex ID (or 'q' to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if raw.lower() == "q":
            break
        try:
            if raw.isdigit():
                s = stats[int(raw)]
            else:
                cid = int(raw, 16)
                s = next((x for x in stats if x["can_id"] == cid), None)
                if s is None:
                    print(f"  ID 0x{cid:X} not found")
                    continue
        except (ValueError, IndexError):
            print("  Invalid input")
            continue

        cid = s["can_id"]
        bsummary = byte_diff_summary(frames, cid)
        print(f"\n  ID 0x{cid:03X}  {s['count']} frames  {s['hz']:.1f} Hz  DLC={s['dlc']}")
        print(f"  {'Byte':>5}  {'Min':>5}  {'Max':>5}  {'Uniq':>6}  {'Changes':>8}")
        for b, info in bsummary.items():
            flag = "  ← varies" if info["changes"] > 5 else ""
            print(f"  {b:5d}  {info['min']:5d}  {info['max']:5d}  "
                  f"{info['n_unique']:6d}  {info['changes']:8d}{flag}")

        try:
            b_raw = input(f"\n  Watch byte (0–{s['dlc']-1}, or Enter to skip): ").strip()
            if b_raw.isdigit():
                watch_bytes(frames, cid, 30)
        except (EOFError, KeyboardInterrupt):
            break
        print()


def main():
    parser = argparse.ArgumentParser(description="CAN signal finder")
    parser.add_argument("log", help="Log file to analyse")
    parser.add_argument("--watch", nargs=2, metavar=("ID", "BYTE"),
                        help="Non-interactive: print ts,value for given ID and byte")
    parser.add_argument("--diff", action="store_true",
                        help="Show all IDs with varying bytes (good starting point)")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        sys.exit(f"File not found: {log_path}")

    print(f"Parsing {log_path}...")
    frames = parse_log(log_path)
    data_frames = [f for f in frames if f["can_id"] is not None]
    markers = [f for f in frames if f["marker"]]
    print(f"  {len(data_frames)} frames, {len(markers)} markers\n")

    stats = id_stats(frames)

    if args.watch:
        cid = int(args.watch[0], 16)
        byte = int(args.watch[1])
        target = [f for f in frames if f["can_id"] == cid and f["data"] and len(f["data"]) > byte]
        print("ts_ms,value")
        for f in target:
            ts = f["ts_ms"] if f["ts_ms"] is not None else 0
            print(f"{ts:.1f},{f['data'][byte]}")
    elif args.diff:
        diff_mode(frames, stats)
    else:
        interactive(frames, stats)


if __name__ == "__main__":
    main()

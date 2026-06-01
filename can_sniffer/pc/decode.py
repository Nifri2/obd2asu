"""
CAN log → CSV timeline decoder for ZC33S confirmed signals.

Confirmed signals (hardcoded — no swift_signals.json needed):
  RPM      0x124  bytes[1:3] BE / 4      (valid when byte0 == 0x08)
  Throttle 0x122  byte4 * 100/255 %      (idle ≈10 %, released <15 %)
  Brake    0x1AF  byte6 & 0x40           (1 when pedal pressed)
  Speed    0x1B8  four 16-bit BE wheels at bytes[0:2,2:4,4:6,6:8] / 32, averaged

Output CSV: t_ms, rpm, throttle_pct, speed_kmh, brake

Synthetic timestamp: counts 0x124 RPM frames at their nominal 50 Hz rate.
Adjust --hz-rpm if your bus uses a different rate.

Log formats auto-detected:
  Mode A  <millis> <hex_id> <dlc> <bytes>
  Mode B  SLCAN  t<III><L><DATA>   (one frame per line, \r optional)
  Mode C  candump  (<ts>) can0 <ID>#<DATA>

Usage:
  python decode.py logs/04_accel_to_second_gear_then_liftoff.log
  python decode.py logs/*.log --out ../../sound_sim/timelines/ --hz-rpm 50
"""

import argparse
import csv
import sys
from pathlib import Path


# ── Signal parsers ────────────────────────────────────────────────────────────

RPM_ID      = 0x124
THROTTLE_ID = 0x122
BRAKE_ID    = 0x1AF
SPEED_ID    = 0x1B8

SIGNAL_IDS = {RPM_ID, THROTTLE_ID, BRAKE_ID, SPEED_ID}


def _parse_signals(can_id: int, data: bytes, state: dict) -> bool:
    """Update state dict from one CAN frame. Returns True if any signal updated."""
    if can_id == RPM_ID:
        if len(data) >= 3 and data[0] == 0x08:
            state["rpm"] = int.from_bytes(data[1:3], "big") / 4.0
            state["_rpm_count"] += 1
            return True
    elif can_id == THROTTLE_ID:
        if len(data) >= 5:
            state["throttle_pct"] = data[4] * 100.0 / 255.0
            return True
    elif can_id == BRAKE_ID:
        if len(data) >= 7:
            state["brake"] = 1 if (data[6] & 0x40) else 0
            return True
    elif can_id == SPEED_ID:
        if len(data) >= 8:
            wheels = [int.from_bytes(data[i*2:(i+1)*2], "big") / 32.0 for i in range(4)]
            state["speed_kmh"] = sum(wheels) / 4.0
            return True
    return False


# ── Log parsers ───────────────────────────────────────────────────────────────

def _detect_format(line: str) -> str:
    if line.startswith("t") or line.startswith("T"):
        return "B"
    if line.startswith("("):
        return "C"
    return "A"


def _parse_slcan_frame(line: str) -> tuple[int, bytes] | None:
    """Parse one SLCAN line → (can_id, data) or None."""
    try:
        if line[0] == "t":
            can_id = int(line[1:4], 16)
            dlc    = int(line[4])
            data   = bytes(int(line[5 + i*2 : 7 + i*2], 16) for i in range(dlc))
        elif line[0] == "T":
            can_id = int(line[1:9], 16)
            dlc    = int(line[9])
            data   = bytes(int(line[10 + i*2 : 12 + i*2], 16) for i in range(dlc))
        else:
            return None
        return can_id, data
    except (ValueError, IndexError):
        return None


def _iter_frames(path: Path):
    """
    Yield (can_id, data, is_rpm_frame: bool, ts_s: float | None) per frame.
    ts_s is set only for Mode C logs; None means synthetic clock will be used.
    """
    fmt = None
    with open(path, errors="replace") as f:
        for raw in f:
            line = raw.strip().lstrip("\r\n")
            if not line or line.startswith("#"):
                continue

            if fmt is None:
                fmt = _detect_format(line)

            try:
                if fmt == "B":
                    result = _parse_slcan_frame(line.rstrip("\r"))
                    if result:
                        yield result[0], result[1], result[0] == RPM_ID, None

                elif fmt == "A":
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    ts_s   = float(parts[0]) / 1000.0
                    can_id = int(parts[1], 16)
                    dlc    = int(parts[2])
                    data   = bytes(int(parts[3 + i], 16) for i in range(dlc))
                    yield can_id, data, can_id == RPM_ID, ts_s

                elif fmt == "C":
                    ts_str = line[1:line.index(")")]
                    ts_s   = float(ts_str)
                    rest   = line[line.index(")") + 1:].strip()
                    parts  = rest.split()
                    id_str, data_str = (parts[1] if len(parts) > 1 else parts[0]).split("#")
                    can_id = int(id_str, 16)
                    data   = bytes.fromhex(data_str)
                    yield can_id, data, can_id == RPM_ID, ts_s

            except (ValueError, IndexError):
                continue


# ── Main decode logic ─────────────────────────────────────────────────────────

def decode_log(path: Path, hz_rpm: float = 50.0) -> list[dict]:
    """
    Parse a log file → list of dicts with t_ms, rpm, throttle_pct, speed_kmh, brake.
    Missing signals carry forward from the last known value.
    Timestamps are either real (Mode A/C) or synthesised from RPM frame count.
    """
    state: dict = {
        "rpm":          0.0,
        "throttle_pct": 0.0,
        "speed_kmh":    0.0,
        "brake":        0,
        "_rpm_count":   0,
    }

    raw_rows: list[dict] = []
    real_ts: list[float | None] = []
    rpm_frame_indices: list[int] = []   # positions of RPM frames in raw_rows

    for frame_idx, (can_id, data, is_rpm, ts_s) in enumerate(_iter_frames(path)):
        updated = _parse_signals(can_id, data, state)
        if updated:
            raw_rows.append({
                "rpm":          state["rpm"],
                "throttle_pct": state["throttle_pct"],
                "speed_kmh":    state["speed_kmh"],
                "brake":        state["brake"],
                "_frame_idx":   frame_idx,
            })
            real_ts.append(ts_s)
            if is_rpm:
                rpm_frame_indices.append(len(raw_rows) - 1)

    if not raw_rows:
        return []

    # ── Assign timestamps ─────────────────────────────────────────────────────
    has_real_ts = any(ts is not None for ts in real_ts)

    if has_real_ts:
        # Mode A or C: real timestamps available; fill gaps by carry-forward.
        t_base = next(ts for ts in real_ts if ts is not None)
        t_ms_list: list[float] = []
        for ts in real_ts:
            if ts is not None:
                t_ms_list.append((ts - t_base) * 1000.0)
            else:
                t_ms_list.append(t_ms_list[-1] + 2.0 if t_ms_list else 0.0)
    else:
        # Mode B (no real timestamps): synthesise using RPM frame count.
        # Each RPM frame nominally arrives at hz_rpm → 1000/hz_rpm ms apart.
        # Build (row_index, t_ms) anchors, then linearly interpolate everything.
        n_rpm  = len(rpm_frame_indices)
        n_rows = len(raw_rows)
        dt_rpm = 1000.0 / hz_rpm   # e.g. 20 ms at 50 Hz

        if n_rpm < 2:
            t_ms_list = [i * 2.0 for i in range(n_rows)]
        else:
            anchors = [(row_idx, k * dt_rpm) for k, row_idx in enumerate(rpm_frame_indices)]
            t_ms_list = [0.0] * n_rows

            # Assign anchor points
            for row_idx, t in anchors:
                t_ms_list[row_idx] = t

            # Interpolate between consecutive anchors
            for k in range(len(anchors) - 1):
                i0, t0 = anchors[k]
                i1, t1 = anchors[k + 1]
                span = i1 - i0
                step = (t1 - t0) / span if span > 0 else dt_rpm / 5
                for j in range(i0 + 1, i1):
                    t_ms_list[j] = t0 + (j - i0) * step

            # Before first anchor: extrapolate backwards using first interval
            i0, t0 = anchors[0]
            if i0 > 0:
                i1, t1 = anchors[1]
                step = (t1 - t0) / max(1, i1 - i0)
                for j in range(i0):
                    t_ms_list[j] = t0 - (i0 - j) * step

            # After last anchor: extrapolate forwards
            i_last, t_last = anchors[-1]
            i_prev, t_prev = anchors[-2]
            step = (t_last - t_prev) / max(1, i_last - i_prev)
            for j in range(i_last + 1, n_rows):
                t_ms_list[j] = t_last + (j - i_last) * step

        # Ensure non-decreasing and shift so min=0
        t_min = min(t_ms_list)
        if t_min < 0.0:
            t_ms_list = [t - t_min for t in t_ms_list]

    rows = []
    for i, row in enumerate(raw_rows):
        rows.append({
            "t_ms":         round(t_ms_list[i], 1),
            "rpm":          round(row["rpm"],          1),
            "throttle_pct": round(row["throttle_pct"], 2),
            "speed_kmh":    round(row["speed_kmh"],    2),
            "brake":        row["brake"],
        })
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    cols = ["t_ms", "rpm", "throttle_pct", "speed_kmh", "brake"]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Decode CAN sniffer log → CSV timeline")
    parser.add_argument("logs",    nargs="+", help="Log file(s) (Mode A, B, or C)")
    parser.add_argument("--out",   default="../../sound_sim/timelines/",
                        help="Output directory for CSV files (default: sound_sim/timelines/)")
    parser.add_argument("--hz-rpm", type=float, default=50.0,
                        help="Nominal RPM frame rate Hz for synthetic clock (default: 50)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for log_arg in args.logs:
        log_path = Path(log_arg)
        if not log_path.exists():
            print(f"Not found: {log_path}", file=sys.stderr)
            continue

        print(f"Decoding {log_path.name}...", end=" ", flush=True)
        rows = decode_log(log_path, hz_rpm=args.hz_rpm)
        if not rows:
            print("no signal frames found — check format or IDs")
            continue

        out_path = out_dir / (log_path.stem + ".csv")
        write_csv(rows, out_path)
        t_span = rows[-1]["t_ms"] - rows[0]["t_ms"]
        rpm_vals = [r["rpm"] for r in rows if r["rpm"] > 100]
        print(f"{len(rows)} rows, {t_span/1000:.1f}s"
              + (f", rpm {min(rpm_vals):.0f}–{max(rpm_vals):.0f}" if rpm_vals else "")
              + f" → {out_path}")


if __name__ == "__main__":
    main()

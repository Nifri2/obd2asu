"""
CAN log decoder — converts a captured log + swift_signals.json → CSV timelines.

Output CSVs go directly into sound_sim/timelines/ and are ready for driving_mode.py.

Usage:
  python decode.py logs/2_cruise.log --signals ../swift_signals.json
  python decode.py logs/2_cruise.log --signals ../swift_signals.json --out ../../sound_sim/timelines/
  python decode.py logs/2_cruise.log --signals ../swift_signals.json --emit-dbc swift.dbc

Log formats auto-detected:
  Mode A: <millis> <hex_id> <dlc> <bytes>
  Mode B: SLCAN t/T frames
  Mode C: candump  (<ts>) can0 <ID>#<DATA>
"""

import argparse
import csv
import json
import struct
import sys
from pathlib import Path


# ── Signal extraction ─────────────────────────────────────────────────────────

def extract_value(data: bytes, byte_offset: int, length: int, endian: str, scale: float, offset: float) -> float | None:
    end = byte_offset + length
    if end > len(data):
        return None
    chunk = data[byte_offset:end]
    fmt_map = {(1, "big"): ">B", (1, "little"): "<B",
               (2, "big"): ">H", (2, "little"): "<H",
               (4, "big"): ">I", (4, "little"): "<I"}
    fmt = fmt_map.get((length, endian))
    if fmt is None:
        return None
    raw = struct.unpack(fmt, chunk)[0]
    return raw * scale + offset


# ── Log parsing (shared with find_signal.py) ──────────────────────────────────

def detect_format(first_line: str) -> str:
    if first_line.startswith("t") or first_line.startswith("T"):
        return "B"
    if first_line.startswith("("):
        return "C"
    return "A"


def parse_log(path: Path) -> list[dict]:
    frames = []
    fmt = None
    first_ts = None

    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
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
                    else:
                        can_id = int(line[1:9], 16)
                        dlc = int(line[9])
                        data = bytes(int(line[10 + i*2:12 + i*2], 16) for i in range(dlc))
                    ts_ms = len(frames) * 1.0
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

                frames.append({"ts_ms": ts_ms, "can_id": can_id, "data": data})
            except (ValueError, IndexError):
                continue

    return frames


# ── Signals config ────────────────────────────────────────────────────────────

def load_signals(path: Path) -> dict:
    with open(path) as f:
        raw = json.load(f)
    # Drop metadata keys starting with _
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def check_signals(signals: dict) -> list[str]:
    warnings = []
    for name, sig in signals.items():
        if sig.get("can_id", "0x000") in ("0x000", "0x0000", "0x00000000"):
            warnings.append(f"  {name}: can_id is still placeholder 0x000 — skipped")
    return warnings


# ── Decode ────────────────────────────────────────────────────────────────────

def decode(frames: list[dict], signals: dict) -> list[dict]:
    """
    Build a timeline dict per CAN ID with latest known values for each signal.
    Output: list of {ts_ms, <signal_name>: value, ...} rows, one per unique timestamp.
    """
    # Group signals by CAN ID
    by_id: dict[int, list[tuple[str, dict]]] = {}
    for name, sig in signals.items():
        cid_raw = sig.get("can_id", "0x000")
        try:
            cid = int(cid_raw, 16)
        except ValueError:
            continue
        if cid == 0:
            continue
        by_id.setdefault(cid, []).append((name, sig))

    # Current known values
    latest: dict[str, float | None] = {n: None for n in signals}
    rows = []

    for f in frames:
        cid = f["can_id"]
        if cid not in by_id:
            continue
        updated = False
        for name, sig in by_id[cid]:
            val = extract_value(
                f["data"],
                sig.get("byte", 0),
                sig.get("length", 1),
                sig.get("endian", "big"),
                sig.get("scale", 1.0),
                sig.get("offset", 0.0),
            )
            if val is not None:
                latest[name] = round(val, 3)
                updated = True
        if updated:
            row = {"ts_ms": round(f["ts_ms"], 1)}
            row.update(latest)
            rows.append(row)

    return rows


def write_csv(rows: list[dict], signals: dict, out_path: Path) -> None:
    # Preferred column order: ts_ms, then required signals, then the rest
    priority = ["ts_ms", "rpm", "throttle", "speed", "load"]
    extras = [k for k in signals if k not in priority]
    columns = priority + extras

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in columns})


# ── DBC emitter ───────────────────────────────────────────────────────────────

def emit_dbc(signals: dict, out_path: Path) -> None:
    lines = ['VERSION ""', "", "NS_ :", "", "BS_:", "", "BU_:", ""]
    groups: dict[int, list] = {}
    for name, sig in signals.items():
        try:
            cid = int(sig.get("can_id", "0x000"), 16)
        except ValueError:
            continue
        if cid == 0:
            continue
        groups.setdefault(cid, []).append((name, sig))

    for cid, sigs in sorted(groups.items()):
        dlc = 8
        lines.append(f"BO_ {cid} {cid:03X}: {dlc} Vector__XXX")
        for name, sig in sigs:
            byte_off = sig.get("byte", 0)
            length   = sig.get("length", 1)
            endian   = sig.get("endian", "big")
            scale    = sig.get("scale", 1.0)
            offset_v = sig.get("offset", 0.0)
            unit     = sig.get("unit", "")
            start_bit = byte_off * 8
            val_type  = "1" if endian == "big" else "0"
            lines.append(
                f" SG_ {name} : {start_bit}|{length*8}@{val_type}+ "
                f"({scale},{offset_v}) [0|0] \"{unit}\" Vector__XXX"
            )
        lines.append("")

    out_path.write_text("\n".join(lines))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Decode CAN log to CSV timeline")
    parser.add_argument("log",       help="Log file (Mode A, B, or C)")
    parser.add_argument("--signals", default="../swift_signals.json", help="Signal table JSON")
    parser.add_argument("--out",     default="../../sound_sim/timelines/", help="Output directory")
    parser.add_argument("--emit-dbc", metavar="FILE", help="Also write a DBC file")
    args = parser.parse_args()

    log_path  = Path(args.log)
    sig_path  = Path(args.signals)
    out_dir   = Path(args.out)

    if not log_path.exists():
        sys.exit(f"Log not found: {log_path}")
    if not sig_path.exists():
        sys.exit(f"Signals file not found: {sig_path}\n"
                 f"Fill in can_sniffer/swift_signals.json first.")

    signals = load_signals(sig_path)
    warnings = check_signals(signals)
    if warnings:
        print("Warnings (signals with placeholder IDs — skipped):")
        for w in warnings:
            print(w)

    active = {k: v for k, v in signals.items()
              if int(v.get("can_id", "0x000"), 16) != 0}
    if not active:
        sys.exit("No usable signals (all can_id values are 0x000). Fill in swift_signals.json first.")

    print(f"Parsing {log_path}...")
    frames = parse_log(log_path)
    print(f"  {len(frames)} frames")

    print(f"Decoding with {len(active)} signals: {', '.join(active.keys())}")
    rows = decode(frames, active)
    print(f"  {len(rows)} output rows")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (log_path.stem + ".csv")
    write_csv(rows, active, out_path)
    print(f"Written: {out_path}")

    if args.emit_dbc:
        dbc_path = Path(args.emit_dbc)
        emit_dbc(signals, dbc_path)
        print(f"DBC: {dbc_path}")


if __name__ == "__main__":
    main()

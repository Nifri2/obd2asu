"""
Driving-mode validator — replay a decoded CSV through the event detector
and play synthesised sounds, exactly as the ESP32 firmware will.

CSV columns expected: t_ms, rpm, throttle_pct, speed_kmh, brake
(output of decode.py — or hand-written with these column names)

Keyboard controls (while replaying):
  1–4    toggle event 1/2/3/4 on/off
  m      toggle master enable
  +/-    raise/lower master volume by 0.1
  r      reload events_config.json thresholds (hot-reload)
  p      pause / resume
  q      quit

Usage:
  cd can_sniffer/pc
  python driving_mode.py ../../sound_sim/timelines/04_accel_to_second_gear_then_liftoff.csv
  python driving_mode.py ../../sound_sim/timelines/*.csv --plot
  python driving_mode.py ... --speed 2.0   # replay at 2× speed
"""

import argparse
import csv
import json
import select
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import numpy as np
import sounddevice as sd

# ── Path setup ────────────────────────────────────────────────────────────────

_HERE  = Path(__file__).parent
_SHARED = _HERE.parent / "shared"
_SOUND  = _HERE.parent.parent / "sound_sim"
sys.path.insert(0, str(_SHARED))
sys.path.insert(0, str(_SOUND))

from detector import EventDetector       # noqa: E402
from events   import build_event, load_config  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

CONFIG_PATH     = _SHARED / "events_config.json"
SOUND_CFG_PATH  = _SOUND  / "config.json"

EVENT_LABELS = {
    "1": "Anti-Lag Snap",
    "2": "Lift-Off Burble",
    "3": "Decel Burble",
    "4": "Soft Upshift",
}
EVENT_COLORS = {"1": "\033[91m", "2": "\033[93m", "3": "\033[95m", "4": "\033[96m"}
RESET        = "\033[0m"
BOLD         = "\033[1m"
DIM          = "\033[2m"


# ── Audio ─────────────────────────────────────────────────────────────────────

def _play_async(buf: np.ndarray, sample_rate: int) -> None:
    def _go():
        sd.play(buf, samplerate=sample_rate)
        sd.wait()
    threading.Thread(target=_go, daemon=True).start()


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_det_cfg(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"\r\n  [warn] could not load {path}: {e}\r\n", end="")
        return {}


def _make_detector(cfg: dict) -> EventDetector:
    return EventDetector(cfg)


# ── CSV loader ────────────────────────────────────────────────────────────────

COLUMN_ALIASES = {
    "throttle":     "throttle_pct",
    "speed":        "speed_kmh",
    "throttle_pct": "throttle_pct",
    "speed_kmh":    "speed_kmh",
}


def _load_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                normalized = {}
                for k, v in row.items():
                    key = COLUMN_ALIASES.get(k, k)
                    normalized[key] = float(v)
                # Require these four fields
                _ = normalized["t_ms"]
                _ = normalized["rpm"]
                _ = normalized["throttle_pct"]
                _ = normalized["speed_kmh"]
                normalized.setdefault("brake", 0.0)
                rows.append(normalized)
            except (KeyError, ValueError):
                continue
    return rows


# ── Status line ───────────────────────────────────────────────────────────────

def _status_bar(det: EventDetector, speed_mult: float, paused: bool) -> str:
    parts = []
    for k, label in EVENT_LABELS.items():
        on = det.enabled.get(k, True)
        col = EVENT_COLORS[k] if on else DIM
        parts.append(f"{col}E{k}{'✓' if on else '✗'}{RESET}")
    master = f"{BOLD}MASTER{'ON' if det.master_enable else 'OFF'}{RESET}"
    vol    = f"VOL {det.master_volume:.1f}"
    spd    = f"×{speed_mult:.1f}"
    status = "PAUSED" if paused else "playing"
    return "  " + " ".join(parts) + f"  {master}  {vol}  {spd}  [{status}]"


# ── Replay engine ─────────────────────────────────────────────────────────────

class DrivingMode:
    def __init__(self, speed: float = 1.0, plot: bool = False):
        self._speed       = speed
        self._plot        = plot
        self._det_cfg     = _load_det_cfg(CONFIG_PATH)
        self._detector    = _make_detector(self._det_cfg)
        self._sound_cfg   = load_config(SOUND_CFG_PATH)
        self._sample_rate = self._sound_cfg.get("sample_rate", 44100)
        self._event_log:  list[tuple[float, str]] = []
        self._paused      = False

    # ── Main entry ────────────────────────────────────────────────────────────

    def run(self, csv_paths: list[Path]) -> None:
        all_rows: list[tuple[Path, list[dict]]] = []
        for p in csv_paths:
            rows = _load_csv(p)
            if rows:
                all_rows.append((p, rows))
            else:
                print(f"  [skip] empty or unreadable: {p}")

        if not all_rows:
            print("No valid CSV files.")
            return

        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setraw(sys.stdin.fileno())
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            for path, rows in all_rows:
                self._replay_file(path, rows)
            sys.stdout.write("\r\nDone.\r\n")
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

        if self._plot and self._event_log:
            self._show_plot(all_rows)

    # ── Per-file replay ───────────────────────────────────────────────────────

    def _replay_file(self, path: Path, rows: list[dict]) -> None:
        # Reset detector for each file
        self._det_cfg   = _load_det_cfg(CONFIG_PATH)
        self._detector  = _make_detector(self._det_cfg)
        self._event_log = []

        sys.stdout.write(f"\r\n{BOLD}  {path.name}{RESET}\r\n  ")
        sys.stdout.write(_status_bar(self._detector, self._speed, self._paused))
        sys.stdout.write("\r\n  (1–4=toggle event  m=master  +/-=volume  r=reload  p=pause  q=quit)\r\n\r\n")
        sys.stdout.flush()

        t0_sim  = rows[0]["t_ms"]
        t0_real = time.monotonic()

        for row in rows:
            # ── Keyboard input (non-blocking) ─────────────────────────────────
            while select.select([sys.stdin], [], [], 0)[0]:
                k = sys.stdin.read(1)
                self._handle_key(k)
                if k.lower() == "q" or k == "\x03":
                    return

            # ── Pause ─────────────────────────────────────────────────────────
            while self._paused:
                while select.select([sys.stdin], [], [], 0.05)[0]:
                    k = sys.stdin.read(1)
                    self._handle_key(k)
                    if k.lower() == "q" or k == "\x03":
                        return

            # ── Wall-clock sync ───────────────────────────────────────────────
            elapsed_sim  = (row["t_ms"] - t0_sim) / self._speed
            elapsed_real = (time.monotonic() - t0_real) * 1000.0
            wait = (elapsed_sim - elapsed_real) / 1000.0
            if wait > 0.001:
                time.sleep(wait)

            # ── Event detection ───────────────────────────────────────────────
            event = self._detector.process(
                row["t_ms"],
                row["rpm"],
                row["throttle_pct"],
                row["speed_kmh"],
                int(row["brake"]),
            )

            if event:
                label  = EVENT_LABELS.get(event, event)
                col    = EVENT_COLORS.get(event, "")
                ts_s   = row["t_ms"] / 1000.0
                sys.stdout.write(
                    f"  t={ts_s:6.2f}s  {col}{BOLD}E{event} {label}{RESET}"
                    f"  RPM={row['rpm']:.0f}  thr={row['throttle_pct']:.0f}%"
                    f"  spd={row['speed_kmh']:.0f}  brk={int(row['brake'])}\r\n"
                )
                sys.stdout.flush()
                self._event_log.append((row["t_ms"], event))
                sound_cfg = self._sound_cfg["events"].get(event)
                if sound_cfg:
                    vol = self._detector.master_volume
                    buf, _ = build_event(sound_cfg, sample_rate=self._sample_rate)
                    if vol < 1.0:
                        buf = (buf * vol).astype(np.float32)
                    _play_async(buf, self._sample_rate)

        sys.stdout.write(
            f"\r\n  {len(self._event_log)} events fired "
            f"({', '.join(f'E{e}' for _, e in self._event_log[:10])}{'…' if len(self._event_log)>10 else ''})\r\n"
        )
        time.sleep(1.5)   # let last sound finish

    # ── Key handler ───────────────────────────────────────────────────────────

    def _handle_key(self, k: str) -> None:
        det = self._detector
        if k in ("1", "2", "3", "4"):
            det.enabled[k] = not det.enabled[k]
            state = "on" if det.enabled[k] else "off"
            sys.stdout.write(f"\r\n  E{k} → {state}\r\n")
        elif k.lower() == "m":
            det.master_enable = not det.master_enable
            sys.stdout.write(f"\r\n  master → {'on' if det.master_enable else 'off'}\r\n")
        elif k in ("+", "="):
            det.master_volume = min(2.0, det.master_volume + 0.1)
            sys.stdout.write(f"\r\n  volume → {det.master_volume:.1f}\r\n")
        elif k == "-":
            det.master_volume = max(0.0, det.master_volume - 0.1)
            sys.stdout.write(f"\r\n  volume → {det.master_volume:.1f}\r\n")
        elif k.lower() == "r":
            self._det_cfg  = _load_det_cfg(CONFIG_PATH)
            new_det        = _make_detector(self._det_cfg)
            # preserve runtime toggles and volume
            new_det.enabled      = dict(det.enabled)
            new_det.master_enable = det.master_enable
            new_det.master_volume = det.master_volume
            self._detector = new_det
            sys.stdout.write("\r\n  config reloaded\r\n")
        elif k.lower() == "p":
            self._paused = not self._paused
            sys.stdout.write(f"\r\n  {'paused' if self._paused else 'resumed'}\r\n")
        sys.stdout.flush()

    # ── Plot ──────────────────────────────────────────────────────────────────

    def _show_plot(self, all_rows: list[tuple[Path, list[dict]]]) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            print("matplotlib not available — skipping plot")
            return

        colors = {"1": "red", "2": "orange", "3": "purple", "4": "cyan"}
        fig, axes = plt.subplots(3, 1, figsize=(14, 8), sharex=True)

        for path, rows in all_rows:
            ts  = [r["t_ms"] / 1000.0 for r in rows]
            rpm = [r["rpm"]            for r in rows]
            thr = [r["throttle_pct"]   for r in rows]
            spd = [r["speed_kmh"]      for r in rows]
            lbl = path.stem
            axes[0].plot(ts, rpm, lw=1, label=lbl)
            axes[1].plot(ts, thr, lw=1, label=lbl)
            axes[2].plot(ts, spd, lw=1, label=lbl)

        for t_ms, ev in self._event_log:
            t_s = t_ms / 1000.0
            col = colors.get(ev, "black")
            for ax in axes:
                ax.axvline(t_s, color=col, lw=1.2, alpha=0.7)
            axes[0].text(t_s, axes[0].get_ylim()[1] * 0.9, f"E{ev}",
                         color=col, fontsize=7, rotation=90, va="top")

        axes[0].set_ylabel("RPM")
        axes[1].set_ylabel("Throttle %")
        axes[2].set_ylabel("Speed km/h")
        axes[2].set_xlabel("Time (s)")
        axes[0].legend(fontsize=7)
        plt.tight_layout()
        plt.show()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Replay CAN timeline through event detector")
    parser.add_argument("csv",    nargs="+", help="CSV file(s) from decode.py")
    parser.add_argument("--plot", action="store_true", help="Show matplotlib plot after replay")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Replay speed multiplier (default 1.0)")
    args = parser.parse_args()

    dm = DrivingMode(speed=args.speed, plot=args.plot)
    dm.run([Path(p) for p in args.csv])


if __name__ == "__main__":
    main()

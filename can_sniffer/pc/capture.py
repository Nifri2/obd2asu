"""
CAN capture tool — reads from ESP32 (USB serial or UDP) and saves to a log file.

Usage:
  python capture.py --port /dev/ttyUSB0 --scenario 7_pedal_sweep
  python capture.py --port /dev/ttyUSB0 --baud 115200 --scenario 2_cruise --out logs/
  python capture.py --udp 4876 --scenario 5_long_decel

Interactive commands while capturing:
  m  → prompt for a marker label, inject into log (and send to firmware if serial)
  q  → quit cleanly
"""

import argparse
import datetime
import select
import socket
import sys
import termios
import threading
import time
import tty
from pathlib import Path


def make_log_path(out_dir: Path, scenario: str) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{scenario}_{ts}.log" if scenario else f"capture_{ts}.log"
    return out_dir / name


# ── Serial source ─────────────────────────────────────────────────────────────

class SerialSource:
    def __init__(self, port: str, baud: int):
        import serial
        self._ser = serial.Serial(port, baud, timeout=0.1)

    def readline(self) -> bytes | None:
        line = self._ser.readline()
        return line if line else None

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    def close(self) -> None:
        self._ser.close()


# ── UDP source ────────────────────────────────────────────────────────────────

class UdpSource:
    def __init__(self, port: int):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("", port))
        self._sock.setblocking(False)
        self._buf = b""

    def readline(self) -> bytes | None:
        try:
            data, _ = self._sock.recvfrom(4096)
            self._buf += data
        except BlockingIOError:
            pass
        if b"\n" in self._buf or b"\r" in self._buf:
            for sep in (b"\r\n", b"\n", b"\r"):
                if sep in self._buf:
                    line, self._buf = self._buf.split(sep, 1)
                    return line + b"\n"
        return None

    def write(self, data: bytes) -> None:
        pass  # UDP is one-way in this setup

    def close(self) -> None:
        self._sock.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CAN capture tool")
    parser.add_argument("--port",     help="Serial port (e.g. /dev/ttyUSB0)")
    parser.add_argument("--baud",     type=int, default=115200)
    parser.add_argument("--udp",      type=int, help="UDP port to listen on")
    parser.add_argument("--scenario", default="", help="Scenario label for filename")
    parser.add_argument("--out",      default="logs", help="Output directory")
    args = parser.parse_args()

    if not args.port and not args.udp:
        parser.error("Provide --port or --udp")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = make_log_path(out_dir, args.scenario)

    if args.port:
        try:
            source = SerialSource(args.port, args.baud)
        except Exception as e:
            sys.exit(f"Cannot open serial port: {e}")
    else:
        source = UdpSource(args.udp)

    print(f"Saving to: {log_path}")
    print("Press 'm' to insert a marker, 'q' to quit.\n")

    frame_count = 0
    t_start = time.monotonic()

    # Raw terminal for single-key input
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin.fileno())

        with open(log_path, "w") as log:
            # Write header
            log.write(f"# CAN capture  scenario={args.scenario}  "
                      f"started={datetime.datetime.now().isoformat()}\r\n")
            log.flush()

            while True:
                # Check for keyboard input (non-blocking)
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if r:
                    k = sys.stdin.read(1)
                    if k.lower() == "q" or k == "\x03":
                        break
                    elif k.lower() == "m":
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        sys.stdout.write("\r\nMarker label: ")
                        sys.stdout.flush()
                        label = sys.stdin.readline().strip()
                        tty.setraw(sys.stdin.fileno())
                        if label:
                            marker_line = f"# MARKER {label}\r\n"
                            log.write(marker_line)
                            log.flush()
                            source.write(f"MARK{label}\n".encode())
                            print(f"\r  [marker: {label}]\r\n", end="")

                # Read from source
                line = source.readline()
                if line:
                    decoded = line.decode("ascii", errors="replace")
                    log.write(decoded)
                    if not decoded.startswith("#"):
                        frame_count += 1
                        # Progress tick every 1000 frames
                        if frame_count % 1000 == 0:
                            elapsed = time.monotonic() - t_start
                            rate = frame_count / elapsed if elapsed > 0 else 0
                            sys.stdout.write(f"\r  {frame_count} frames  {rate:.0f} fps  {elapsed:.0f}s")
                            sys.stdout.flush()
                    log.flush()

    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        source.close()

    elapsed = time.monotonic() - t_start
    rate = frame_count / elapsed if elapsed > 0 else 0
    print(f"\n\nDone. {frame_count} frames in {elapsed:.1f}s ({rate:.0f} fps)")
    print(f"Log: {log_path}")


if __name__ == "__main__":
    main()

"""
CAN Sniffer — MicroPython firmware for ESP32 + MCP2515
======================================================
SAFETY: listen-only mode — the MCP2515 never transmits. Fully passive.

Modes (send 'A', 'B', or 'C' over serial REPL to switch — no newline needed):
  A  Raw dump     <millis> <hex_id> <dlc> <b0> <b1> ...
  B  SLCAN ASCII  t<ID3><DLC><DATA>\r  (SavvyCAN / python-can compatible)
  C  candump -tz  (<ts.us>) can0 <ID>#<DATA>  + # MARKER lines

Marker injection (Mode C): type 'MARK<label>' then Enter in the REPL.
Marker button: press GPIO0 (BOOT button on DevKitC) to inject '# MARKER BTN'.
"""

import sys
import time
import uselect
from machine import SPI, Pin

import config
from mcp_driver import MCP2515

# ── Init ──────────────────────────────────────────────────────────────────────

spi = SPI(1, baudrate=config.SPI_BAUDRATE, polarity=0, phase=0,
          sck=Pin(config.SCK_PIN),
          mosi=Pin(config.MOSI_PIN),
          miso=Pin(config.MISO_PIN))

cs  = Pin(config.CS_PIN, Pin.OUT, value=1)
can = MCP2515(spi, cs)

btn     = Pin(config.MARKER_BTN_PIN, Pin.IN, Pin.PULL_UP)
btn_was = btn.value()

if not can.init_listen_only(config.CNF1, config.CNF2, config.CNF3):
    sys.stdout.write("# FAIL: MCP2515 did not enter listen-only mode\r\n")
    sys.stdout.write("#   Run smoke_test.py to diagnose SPI wiring.\r\n")
    raise SystemExit

sys.stdout.write(f"# MCP2515 ready — listen-only, {config.CAN_BITRATE // 1000}K baud\r\n")
sys.stdout.write("# Send A / B / C to switch mode. MARK<label> to inject marker (Mode C).\r\n")

# ── State ─────────────────────────────────────────────────────────────────────

mode         = config.DEFAULT_MODE
t_boot_ms    = time.ticks_ms()
t_last_frame = time.ticks_ms()
cmd_buf      = bytearray()

# ── Helpers ───────────────────────────────────────────────────────────────────

def emit(s):
    sys.stdout.write(s)

def emit_marker(label):
    emit(f"# MARKER {label}\r\n")

def format_frame(can_id, dlc, data, ext):
    ms = time.ticks_diff(time.ticks_ms(), t_boot_ms)
    if mode == 'A':
        hex_bytes = ' '.join(f'{b:02X}' for b in data)
        return f"{ms} {can_id:03X} {dlc} {hex_bytes}\r\n"
    elif mode == 'B':
        hex_data = ''.join(f'{b:02X}' for b in data)
        return (f"T{can_id:08X}{dlc}{hex_data}\r" if ext
                else f"t{can_id:03X}{dlc}{hex_data}\r")
    else:  # C
        ts_s = ms / 1000.0
        hex_data = ''.join(f'{b:02X}' for b in data)
        return f"({ts_s:.6f}) can0 {can_id:03X}#{hex_data}\r\n"

# ── Serial command handler ────────────────────────────────────────────────────

_poll = uselect.poll()
_poll.register(sys.stdin, uselect.POLLIN)

def process_serial():
    global mode
    while _poll.poll(0):
        c = sys.stdin.read(1)
        if c in ('\r', '\n'):
            line = cmd_buf.decode().strip()
            del cmd_buf[:]
            if line in ('A', 'B', 'C'):
                mode = line
                emit(f"# Mode -> {mode}\r\n")
            elif line.upper().startswith('MARK'):
                emit_marker(line[4:])
        else:
            # Accept single-char mode switch without newline
            if len(cmd_buf) == 0 and c in ('A', 'B', 'C'):
                mode = c
                emit(f"# Mode -> {mode}\r\n")
            else:
                cmd_buf.extend(c.encode())

# ── Main loop ─────────────────────────────────────────────────────────────────

while True:
    process_serial()

    # Marker button (active-low, BOOT button = GPIO0)
    btn_now = btn.value()
    if btn_now == 0 and btn_was == 1:
        emit_marker("BTN")
    btn_was = btn_now

    # Receive frame
    frame = can.recv()
    if frame:
        t_last_frame = time.ticks_ms()
        emit(format_frame(*frame))
        continue

    # Timeout warning
    if time.ticks_diff(time.ticks_ms(), t_last_frame) > 10_000:
        emit("# TIMEOUT — no frames for 10s. Check baud / CANH-CANL wiring.\r\n")
        t_last_frame = time.ticks_ms()

    time.sleep_us(200)

"""
main_soundsystem.py — ZC33S active sound system (MicroPython / ESP32).

Architecture:
  Thread 0 (main): CAN → signals → EventDetector → event queue + serial cmds
  Thread 1 (sound): pops from queue → I2S via SoundEngine

Serial commands (no newline needed for E/MASTER/VOL):
  E1 on / E1 off       toggle individual event
  E2 on …              (same for E2, E3, E4)
  MASTER on / off      mute everything
  VOL 0.7              set master volume 0.0–1.0
  STATUS               print current state

Safe failsafe:
  • No CAN frame for >2 s → mute (resume when frames return)
  • Boot: ignore first 2 s (engine start / ECU wake-up noise)

Upload checklist (mpremote):
  mpremote cp can_sniffer/firmware/mcp_driver.py       :/mcp_driver.py
  mpremote cp can_sniffer/firmware/config.py            :/config.py
  mpremote cp can_sniffer/firmware/sound_engine.py      :/sound_engine.py
  mpremote cp can_sniffer/shared/detector.py            :/detector.py
  mpremote cp can_sniffer/firmware/main_soundsystem.py  :/main.py
  mpremote mkdir /pops
  mpremote cp -r can_sniffer/pops/ :/pops/
"""

import _thread
import json
import sys
import time
import uselect

from machine import SPI, Pin

import config
from mcp_driver  import MCP2515
from sound_engine import SoundEngine
from detector     import EventDetector


# ── Runtime state (shared between threads) ───────────────────────────────────

_RUNTIME_FILE = "/runtime.json"

_runtime = {
    "enabled":       {"1": True, "2": True, "3": True, "4": True},
    "master_enable": True,
    "master_volume": 1.0,
}

_event_queue: list    = []   # list of (event_id: str, volume: float)
_queue_lock           = _thread.allocate_lock()
_QUEUE_MAX            = 4    # cap pending events; oldest dropped if full


# ── Persist runtime config ────────────────────────────────────────────────────

def _load_runtime() -> None:
    try:
        with open(_RUNTIME_FILE) as f:
            saved = json.load(f)
            _runtime["enabled"].update(saved.get("enabled", {}))
            _runtime["master_enable"] = bool(saved.get("master_enable", True))
            _runtime["master_volume"] = float(saved.get("master_volume", 1.0))
    except Exception:
        pass   # no saved config — use defaults


def _save_runtime() -> None:
    try:
        with open(_RUNTIME_FILE, "w") as f:
            json.dump(_runtime, f)
    except Exception:
        pass


# ── Serial command handler ────────────────────────────────────────────────────

def _handle_command(line: str) -> None:
    parts = line.strip().upper().split()
    if not parts:
        return

    if parts[0] == "STATUS":
        en_str = " ".join(f"E{k}={'on' if v else 'off'}"
                          for k, v in sorted(_runtime["enabled"].items()))
        sys.stdout.write(
            f"# {en_str}  master={'on' if _runtime['master_enable'] else 'off'}"
            f"  vol={_runtime['master_volume']:.2f}\r\n"
        )
        return

    if len(parts) == 2:
        cmd, arg = parts[0], parts[1]

        if cmd.startswith("E") and cmd[1:] in ("1", "2", "3", "4"):
            key = cmd[1:]
            val = (arg == "ON")
            _runtime["enabled"][key] = val
            _save_runtime()
            sys.stdout.write(f"# E{key} {'on' if val else 'off'}\r\n")
            return

        if cmd == "MASTER":
            _runtime["master_enable"] = (arg == "ON")
            _save_runtime()
            sys.stdout.write(f"# master {'on' if _runtime['master_enable'] else 'off'}\r\n")
            return

        if cmd == "VOL":
            try:
                _runtime["master_volume"] = max(0.0, min(1.0, float(arg)))
                _save_runtime()
                sys.stdout.write(f"# vol {_runtime['master_volume']:.2f}\r\n")
            except ValueError:
                sys.stdout.write("# VOL: expected 0.0–1.0\r\n")
            return

    sys.stdout.write("# cmds: E1/E2/E3/E4 on|off  MASTER on|off  VOL 0.0–1.0  STATUS\r\n")


# ── CAN signal state ──────────────────────────────────────────────────────────

_sig = {"rpm": 0.0, "throttle": 0.0, "speed": 0.0, "brake": 0}


def _parse_frame(can_id: int, data: bytes) -> None:
    if can_id == 0x124 and len(data) >= 3 and data[0] == 0x08:
        _sig["rpm"] = int.from_bytes(data[1:3], "big") / 4.0

    elif can_id == 0x122 and len(data) >= 5:
        _sig["throttle"] = data[4] * 100.0 / 255.0

    elif can_id == 0x1AF and len(data) >= 7:
        _sig["brake"] = 1 if (data[6] & 0x40) else 0

    elif can_id == 0x1B8 and len(data) >= 8:
        speeds = [(data[i*2] << 8 | data[i*2 + 1]) / 32.0 for i in range(4)]
        _sig["speed"] = sum(speeds) / 4.0


# ── Sound thread (Thread 1) ───────────────────────────────────────────────────

def _sound_thread() -> None:
    engine = SoundEngine(
        sck_pin = config.I2S_BCK_PIN,
        ws_pin  = config.I2S_WS_PIN,
        sd_pin  = config.I2S_SD_PIN,
        rate    = config.I2S_RATE,
        ibuf    = config.I2S_IBUF,
    )
    engine.init_i2s()
    if not engine.load_pops(config.POPS_DIR):
        sys.stdout.write("# WARNING: pop bank empty — no audio will play\r\n")

    sys.stdout.write("# sound thread running\r\n")

    while True:
        item = None
        with _queue_lock:
            if _event_queue:
                item = _event_queue.pop(0)

        if item:
            event_id, volume = item
            engine.master_volume = _runtime["master_volume"]
            engine.play(event_id, volume=volume)
        else:
            # Keep DMA buffer fed with silence to avoid I2S underflow clicks.
            engine.write_silence(256)


# ── CAN / detection thread (Thread 0 / main) ─────────────────────────────────

def _can_main() -> None:
    # Load detector config from filesystem
    try:
        with open("/shared/events_config.json") as f:
            det_cfg = json.load(f)
    except Exception:
        det_cfg = {}

    detector = EventDetector(det_cfg)

    # Apply persisted toggle state
    for k in ("1", "2", "3", "4"):
        detector.enabled[k]  = _runtime["enabled"].get(k, True)
    detector.master_enable   = _runtime["master_enable"]
    detector.master_volume   = _runtime["master_volume"]

    # Init MCP2515 CAN controller
    spi = SPI(
        1,
        baudrate = config.SPI_BAUDRATE,
        polarity = 0,
        phase    = 0,
        sck      = Pin(config.SCK_PIN),
        mosi     = Pin(config.MOSI_PIN),
        miso     = Pin(config.MISO_PIN),
    )
    cs  = Pin(config.CS_PIN, Pin.OUT, value=1)
    can = MCP2515(spi, cs)

    if not can.init_listen_only(config.CNF1, config.CNF2, config.CNF3):
        sys.stdout.write("# FAIL: MCP2515 did not enter listen-only mode\r\n")
        sys.stdout.write("#   Run can_sniffer/firmware/sniffer.py to diagnose.\r\n")
        return

    sys.stdout.write(f"# MCP2515 ready — {config.CAN_BITRATE // 1000}K listen-only\r\n")
    sys.stdout.write("# Sound system started. Serial cmds: E1 on|off  MASTER on|off  VOL 0.x  STATUS\r\n")

    # Serial input
    _poll    = uselect.poll()
    _poll.register(sys.stdin, uselect.POLLIN)
    cmd_buf  = bytearray()

    t_boot   = time.ticks_ms()
    t_last_f = time.ticks_ms()   # last CAN frame received
    muted    = False              # failsafe mute flag

    while True:
        # ── Serial command reader ─────────────────────────────────────────────
        while _poll.poll(0):
            c = sys.stdin.read(1)
            if c in ('\r', '\n'):
                _handle_command(cmd_buf.decode(errors="replace"))
                # Propagate toggle changes to detector
                for k in ("1", "2", "3", "4"):
                    detector.enabled[k] = _runtime["enabled"].get(k, True)
                detector.master_enable = _runtime["master_enable"]
                detector.master_volume = _runtime["master_volume"]
                del cmd_buf[:]
            elif len(cmd_buf) < 64:
                cmd_buf.extend(c.encode())

        # ── CAN receive ───────────────────────────────────────────────────────
        frame = can.recv()
        if frame:
            can_id, dlc, data, ext = frame
            t_last_f = time.ticks_ms()
            _parse_frame(can_id, data)

        # ── Failsafe: mute if CAN silent for >2 s ─────────────────────────────
        no_frame_ms = time.ticks_diff(time.ticks_ms(), t_last_f)
        if no_frame_ms > 2000:
            if not muted:
                sys.stdout.write("# FAILSAFE: no CAN for 2s — muted\r\n")
                muted = True
            time.sleep_us(1000)
            continue
        if muted:
            sys.stdout.write("# CAN resumed — unmuted\r\n")
            muted = False

        # ── Boot grace period: ignore first 2 s ───────────────────────────────
        if time.ticks_diff(time.ticks_ms(), t_boot) < 2000:
            time.sleep_us(500)
            continue

        # ── Event detection ───────────────────────────────────────────────────
        t_ms  = float(time.ticks_diff(time.ticks_ms(), t_boot))
        event = detector.process(
            t_ms,
            _sig["rpm"],
            _sig["throttle"],
            _sig["speed"],
            _sig["brake"],
        )

        if event and _runtime["master_enable"] and _runtime["enabled"].get(event, False):
            vol = _runtime["master_volume"]
            with _queue_lock:
                if len(_event_queue) < _QUEUE_MAX:
                    sys.stdout.write(f"# POP event={event} vol={vol:.2f}\r\n")
                    _event_queue.append((event, vol))
                else:
                    sys.stdout.write(f"# POP DROPPED (queue full) event={event}\r\n")

        time.sleep_us(500)


# ── Entry point ───────────────────────────────────────────────────────────────

_load_runtime()
_thread.start_new_thread(_sound_thread, ())
time.sleep_ms(500)   # give sound thread time to init I2S before CAN starts
_can_main()

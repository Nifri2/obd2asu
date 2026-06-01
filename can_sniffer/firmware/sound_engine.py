"""
sound_engine.py — I2S audio engine for MicroPython / ESP32.

Streams pre-rendered pop primitives from flash (int16 mono .pcm files)
through I2S to the PCM5102A DAC with volume scaling.

Files live in POPS_DIR (default /pops/), populated by running
  python sound_sim/export.py --fmt raw --rate 22050 --out can_sniffer/pops/
on the PC, then uploading the whole pops/ folder to the device.

Designed to run in a dedicated thread (Core 1 side).  I2S.write() releases
the GIL while waiting for DMA space, so the CAN reader on the other thread
remains unblocked.
"""

import json
import os
import sys
import time

import micropython

try:
    from machine import I2S, Pin
    _HAS_I2S = True
except ImportError:
    _HAS_I2S = False   # running on PC simulator


# ── Viper inner-loop for mono→stereo+volume conversion ───────────────────────
# Falls back to pure Python if @micropython.viper is unavailable.

try:
    @micropython.viper
    def _cvt(src: ptr8, dst: ptr8, n_bytes: int, vol_fp: int):
        """Convert n_bytes of int16 mono to stereo, apply 8-bit fixed-point vol."""
        i: int = 0
        j: int = 0
        while i < n_bytes:
            lo: int = int(src[i])
            hi: int = int(src[i + 1])
            s: int  = lo | (hi << 8)
            if s >= 32768:
                s -= 65536
            s = (s * vol_fp) >> 8
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            u: int = s & 0xFFFF
            dst[j]     = u & 0xFF
            dst[j + 1] = (u >> 8) & 0xFF
            dst[j + 2] = u & 0xFF
            dst[j + 3] = (u >> 8) & 0xFF
            i += 2
            j += 4

    _VIPER_OK = True

except Exception:
    _VIPER_OK = False

    def _cvt(src, dst, n_bytes, vol_fp):
        j = 0
        for i in range(0, n_bytes, 2):
            s = src[i] | (src[i + 1] << 8)
            if s >= 32768:
                s -= 65536
            s = (s * vol_fp) >> 8
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            u = s & 0xFFFF
            dst[j]     = u & 0xFF
            dst[j + 1] = (u >> 8) & 0xFF
            dst[j + 2] = u & 0xFF
            dst[j + 3] = (u >> 8) & 0xFF
            j += 4


# ── SoundEngine ───────────────────────────────────────────────────────────────

class SoundEngine:
    # Mono chunk size in bytes (int16 → 2 bytes/sample).
    # 256 bytes = 128 samples ≈ 5.8 ms at 22050 Hz — small enough for quick
    # iteration, large enough to amortise Python overhead per write().
    CHUNK_MONO  = 256
    CHUNK_STER  = CHUNK_MONO * 2    # stereo output bytes per chunk

    def __init__(self, sck_pin: int, ws_pin: int, sd_pin: int,
                 rate: int = 22050, ibuf: int = 8192):
        self._rate   = rate
        self._ibuf   = ibuf
        self._i2s    = None
        self._sck_pin = sck_pin
        self._ws_pin  = ws_pin
        self._sd_pin  = sd_pin

        self._manifest: dict        = {}
        self._pops_dir: str         = "/pops"
        self._silence: bytearray    = bytearray(self.CHUNK_STER)
        self._mono_buf: bytearray   = bytearray(self.CHUNK_MONO)
        self._ster_buf: bytearray   = bytearray(self.CHUNK_STER)

        self.master_volume: float   = 1.0

    def init_i2s(self) -> None:
        if not _HAS_I2S:
            sys.stdout.write("# sound_engine: no I2S (running on PC?)\r\n")
            return
        self._i2s = I2S(
            0,
            sck  = Pin(self._sck_pin),
            ws   = Pin(self._ws_pin),
            sd   = Pin(self._sd_pin),
            mode = I2S.TX,
            bits = 16,
            format = I2S.STEREO,
            rate   = self._rate,
            ibuf   = self._ibuf,
        )
        sys.stdout.write(f"# I2S init: BCK={self._sck_pin} WS={self._ws_pin} "
                         f"SD={self._sd_pin} {self._rate}Hz\r\n")

    def load_pops(self, pops_dir: str = "/pops") -> bool:
        self._pops_dir = pops_dir
        manifest_path  = pops_dir + "/manifest.json"
        try:
            with open(manifest_path) as f:
                self._manifest = json.load(f)
            n = sum(len(v["files"]) for v in self._manifest.get("events", {}).values())
            sys.stdout.write(f"# pop bank loaded: {n} files from {pops_dir}\r\n")
            return True
        except Exception as e:
            sys.stdout.write(f"# pop bank load failed: {e}\r\n")
            self._manifest = {}
            return False

    def play(self, event_id: str, volume: float = 1.0) -> None:
        """
        Stream the pop for event_id from flash through I2S.
        Picks a random variant from the bank.
        Blocks until the file has been fully pushed to the DMA buffer —
        the actual audio continues playing in hardware after this returns.
        """
        info = self._manifest.get("events", {}).get(str(event_id))
        if not info:
            return

        files = info["files"]
        # Cheap random using time ticks lower bits
        idx   = time.ticks_ms() % len(files)
        fpath = self._pops_dir + "/" + files[idx]

        vol_fp = max(0, min(256, int(volume * self.master_volume * 256)))

        try:
            with open(fpath, "rb") as fh:
                while True:
                    n = fh.readinto(self._mono_buf)
                    if not n:
                        break
                    _cvt(self._mono_buf, self._ster_buf, n, vol_fp)
                    if self._i2s:
                        self._i2s.write(self._ster_buf[:n * 2])
        except OSError:
            pass

    def write_silence(self, n_samples: int = 512) -> None:
        """Write silence to keep the DMA buffer fed when nothing is playing."""
        if not self._i2s:
            return
        remaining = n_samples * 4   # stereo int16 = 4 bytes/sample
        while remaining > 0:
            chunk = min(remaining, len(self._silence))
            self._i2s.write(self._silence[:chunk])
            remaining -= chunk

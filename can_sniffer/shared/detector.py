"""
EventDetector — ZC33S confirmed signals.

Pure Python, no numpy. Designed to copy verbatim to MicroPython firmware
(swap `from collections import deque` for `from ucollections import deque`).

Inputs per sample: (ts_ms, rpm, throttle_pct, speed_kmh, brake:int 0/1)
Returns: event key "1"–"4", or None.

Signal origins:
  rpm       0x124 bytes[1:3] BE / 4         (valid when byte0 == 0x08)
  throttle  0x122 byte4 * 100/255           (idle ≈10%, released <15%)
  speed     0x1B8 four 16-bit BE wheels /32, averaged
  brake     0x1AF byte6 & 0x40              (set when brake pedal pressed)
"""

from collections import deque


class EventDetector:
    HISTORY = 20

    def __init__(self, cfg: dict | None = None):
        cfg  = cfg or {}
        t    = cfg.get("thresholds", {})

        # ── E1 Anti-Lag Snap ──────────────────────────────────────────────────
        self.e1_rpm_min      = t.get("e1_rpm_min",      4500)
        self.e1_throttle_was = t.get("e1_throttle_was",   60)
        self.e1_throttle_now = t.get("e1_throttle_now",   15)
        self.e1_drpm_dt      = t.get("e1_drpm_dt",      -300)
        self.e1_dthr_dt      = t.get("e1_dthr_dt",      -200)

        # ── E2 Lift-Off Burble ────────────────────────────────────────────────
        self.e2_rpm_min      = t.get("e2_rpm_min",      2500)
        self.e2_rpm_max      = t.get("e2_rpm_max",      4500)
        self.e2_throttle_lo  = t.get("e2_throttle_lo",    20)
        self.e2_throttle_hi  = t.get("e2_throttle_hi",    60)
        self.e2_throttle_now = t.get("e2_throttle_now",   15)
        self.e2_dthr_lo      = t.get("e2_dthr_lo",       -30)  # not too fast
        self.e2_dthr_hi      = t.get("e2_dthr_hi",      -250)  # not too slow
        self.e2_speed_min    = t.get("e2_speed_min",       30)

        # ── E3 Decel Burble ───────────────────────────────────────────────────
        self.e3_throttle     = t.get("e3_throttle",       15)
        self.e3_rpm_min      = t.get("e3_rpm_min",      2000)
        self.e3_speed_min    = t.get("e3_speed_min",       20)
        self.e3_sustain_ms   = t.get("e3_sustain_ms",     300)

        # ── E4 Soft Upshift ───────────────────────────────────────────────────
        self.e4_rpm_was_max  = t.get("e4_rpm_was_max",  3000)
        self.e4_speed_min    = t.get("e4_speed_min",       10)
        self.e4_dthr_hi      = t.get("e4_dthr_hi",        30)   # throttle near-closed
        self.e4_drpm_dt      = t.get("e4_drpm_dt",        -80)

        cds = cfg.get("cooldowns_ms", {})
        self._cooldowns = {
            "1": cds.get("1", 1000),
            "2": cds.get("2",  400),
            "3": cds.get("3", 2000),
            "4": cds.get("4",  600),
        }
        self._history    = deque(maxlen=self.HISTORY)
        self._last_fired = {"1": -999999.0, "2": -999999.0,
                            "3": -999999.0, "4": -999999.0}

        # E3 throttle-low sustain tracking
        self._throttle_low_since: float | None = None

        # Per-event toggles and master
        en = cfg.get("enabled", {})
        self.enabled = {
            "1": bool(en.get("1", True)),
            "2": bool(en.get("2", True)),
            "3": bool(en.get("3", True)),
            "4": bool(en.get("4", True)),
        }
        self.master_enable = bool(cfg.get("master_enable", True))
        self.master_volume = float(cfg.get("master_volume", 1.0))

    # ── Public API ─────────────────────────────────────────────────────────────

    def process(self, ts_ms: float, rpm: float, throttle: float,
                speed: float, brake: int) -> str | None:
        """
        Feed one sample. Returns fired event key or None.
        brake=1 suppresses E2 and E3.
        """
        if not self.master_enable:
            return None

        self._history.append((ts_ms, rpm, throttle, speed, brake))
        if len(self._history) < 3:
            return None

        drpm_dt, dthr_dt = self._derivatives()
        prev = self._prev()

        # Update E3 sustain tracker
        if throttle < self.e3_throttle:
            if self._throttle_low_since is None:
                self._throttle_low_since = ts_ms
        else:
            self._throttle_low_since = None

        event = None

        # E1: hard full-throttle → sudden full lift at high RPM
        if self.enabled["1"] and self._ready("1", ts_ms):
            if (prev["rpm"]      > self.e1_rpm_min and
                    prev["throttle"] > self.e1_throttle_was and
                    throttle         < self.e1_throttle_now and
                    dthr_dt          < self.e1_dthr_dt and
                    not brake):
                event = "1"

        # E1 blocks E2 and E4
        if event != "1":
            # E2: moderate lift, mid-RPM, controlled drop rate
            if self.enabled["2"] and self._ready("2", ts_ms) and not brake:
                if (self.e2_rpm_min       < prev["rpm"]      < self.e2_rpm_max and
                        self.e2_throttle_lo  < prev["throttle"] < self.e2_throttle_hi and
                        throttle             < self.e2_throttle_now and
                        self.e2_dthr_hi      < dthr_dt            < self.e2_dthr_lo and
                        speed                > self.e2_speed_min):
                    event = "2"

            # E4: soft upshift — clean RPM drop, throttle near-closed, low RPM
            if event is None and self.enabled["4"] and self._ready("4", ts_ms):
                if (prev["rpm"]  < self.e4_rpm_was_max and
                        drpm_dt  < self.e4_drpm_dt and
                        throttle < self.e4_dthr_hi and
                        speed    > self.e4_speed_min):
                    event = "4"

        # E3: sustained closed-throttle overrun
        if event is None and self.enabled["3"] and self._ready("3", ts_ms) and not brake:
            low_ms = (ts_ms - self._throttle_low_since) if self._throttle_low_since else 0.0
            if (throttle < self.e3_throttle and
                    rpm     > self.e3_rpm_min  and
                    speed   > self.e3_speed_min and
                    low_ms  >= self.e3_sustain_ms):
                event = "3"

        if event is not None:
            self._last_fired[event] = ts_ms
        return event

    def set_enabled(self, key: str, val: bool) -> None:
        if key in self.enabled:
            self.enabled[key] = val

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _ready(self, key: str, ts_ms: float) -> bool:
        return ts_ms - self._last_fired[key] >= self._cooldowns[key]

    def _prev(self) -> dict:
        ts, rpm, thr, spd, brk = self._history[-2]
        return {"ts": ts, "rpm": rpm, "throttle": thr, "speed": spd, "brake": brk}

    def _derivatives(self) -> tuple[float, float]:
        """drpm/dt and dthrottle/dt in units/second from last two samples."""
        t1, rpm1, th1, _, _ = self._history[-2]
        t2, rpm2, th2, _, _ = self._history[-1]
        dt_s = (t2 - t1) / 1000.0
        if dt_s < 1e-6:
            return 0.0, 0.0
        return (rpm2 - rpm1) / dt_s, (th2 - th1) / dt_s

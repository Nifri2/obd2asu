"""
Burble / Crackle POC (Wayland / Terminal Safe)
==============================================
Keys:
  1 → Anti-lag SNAP  (loud, aggressive, rapid-fire pops)
  2 → Decel BURBLE   (sustained irregular crackling)
  3 → Soft SNAP      (gentle single/double pop, <3000rpm feel)
  q → Quit

Requires:  pip install numpy sounddevice
"""

import sys
import tty
import termios
import threading
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 44100


# ---------------------------------------------------------------------------
# Core synthesis primitives
# ---------------------------------------------------------------------------

def make_pop(
    duration: float = 0.04,
    freq_low: float = 80.0,
    freq_high: float = 220.0,
    decay: float = 60.0,
    volume: float = 1.0,
    noise_mix: float = 0.75,
) -> np.ndarray:
    """
    Single exhaust pop — band-limited noise burst + low-frequency thump,
    shaped by an exponential decay envelope.
    """
    n = int(SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)

    # Noise component (gives the hiss/crack texture)
    noise = np.random.randn(n)

    # Tonal thump underneath (gives body)
    freq = np.random.uniform(freq_low, freq_high)
    tone = np.sin(2.0 * np.pi * freq * t)

    # Mix and shape
    raw = noise * noise_mix + tone * (1.0 - noise_mix)
    envelope = np.exp(-decay * t)
    pop = raw * envelope * volume

    return pop.astype(np.float32)


def place_pops(
    pops: list,
    offsets_s: list,
    total_duration: float,
) -> np.ndarray:
    """
    Place a list of pop arrays at given time offsets into a buffer.
    Overlapping pops accumulate naturally (no clipping guard yet — keep
    volumes sane).
    """
    total_samples = int(SAMPLE_RATE * total_duration)
    buf = np.zeros(total_samples, dtype=np.float32)

    for pop, offset in zip(pops, offsets_s):
        start = int(offset * SAMPLE_RATE)
        end = min(start + len(pop), total_samples)
        chunk = pop[: end - start]
        buf[start:end] += chunk

    return buf


def normalise(buf: np.ndarray, headroom: float = 0.92) -> np.ndarray:
    peak = np.max(np.abs(buf))
    if peak > 1e-6:
        buf = buf / peak * headroom
    return buf


# ---------------------------------------------------------------------------
# The three sounds
# ---------------------------------------------------------------------------

def sound_antilag_snap() -> np.ndarray:
    """
    Loud, violent anti-lag style crack.
    3–5 rapid overlapping pops, front-loaded, with a hard initial transient.
    """
    rng = np.random.default_rng()
    n_pops = rng.integers(4, 7)

    # First pop hits immediately and hardest
    pops = [make_pop(duration=0.055, freq_low=60, freq_high=180,
                     decay=45, volume=1.0, noise_mix=0.85)]
    offsets = [0.0]

    # Rapid follow-up pops within ~150ms
    t = 0.0
    for _ in range(n_pops - 1):
        t += rng.uniform(0.018, 0.040)
        vol = rng.uniform(0.55, 0.90)
        pops.append(make_pop(duration=0.050, freq_low=70, freq_high=200,
                              decay=55, volume=vol, noise_mix=0.80))
        offsets.append(t)

    buf = place_pops(pops, offsets, total_duration=0.45)
    return normalise(buf, headroom=0.95)


def sound_decel_burble() -> np.ndarray:
    """
    Sustained decel burble — irregular stream of softer pops over ~1.4s,
    tapering off toward the end as RPM drops.
    """
    rng = np.random.default_rng()
    total = 1.5

    pops = []
    offsets = []
    t = 0.0
    fade_start = 0.9  # after this point, pops get quieter

    while t < total - 0.1:
        # Gap between pops — irregular, tighter at the start
        gap = rng.uniform(0.055, 0.130)
        t += gap

        # Volume fades toward end (simulates RPM dropping)
        fade = max(0.2, 1.0 - (t / total) * 0.75) if t > fade_start else 1.0
        vol = rng.uniform(0.35, 0.65) * fade

        pops.append(make_pop(
            duration=rng.uniform(0.030, 0.060),
            freq_low=90,
            freq_high=160,
            decay=rng.uniform(50, 80),
            volume=vol,
            noise_mix=0.70,
        ))
        offsets.append(t)

    buf = place_pops(pops, offsets, total_duration=total)
    return normalise(buf, headroom=0.80)


def sound_soft_snap() -> np.ndarray:
    """
    Gentle upshift snap below 3000rpm — one or two polite pops,
    higher pitched, shorter decay, lower volume.
    """
    rng = np.random.default_rng()
    n_pops = rng.integers(1, 3)

    pops = [make_pop(duration=0.035, freq_low=120, freq_high=260,
                     decay=80, volume=0.60, noise_mix=0.65)]
    offsets = [0.0]

    if n_pops == 2:
        pops.append(make_pop(duration=0.030, freq_low=130, freq_high=270,
                              decay=90, volume=0.35, noise_mix=0.60))
        offsets.append(rng.uniform(0.045, 0.080))

    buf = place_pops(pops, offsets, total_duration=0.30)
    return normalise(buf, headroom=0.65)


# ---------------------------------------------------------------------------
# Playback — non-blocking so rapid keypresses stack naturally
# ---------------------------------------------------------------------------

def play(buf: np.ndarray) -> None:
    """Fire-and-forget playback in a daemon thread."""
    def _play():
        sd.play(buf, samplerate=SAMPLE_RATE)
        sd.wait()

    t = threading.Thread(target=_play, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Terminal listener and Main Loop
# ---------------------------------------------------------------------------

LABELS = {
    "1": "🔥 Anti-lag SNAP",
    "2": "🌀 Decel BURBLE",
    "3": "💨 Soft SNAP",
}

def main():
    print("\n" + "=" * 42)
    print("  Burble / Crackle POC")
    print("=" * 42)
    print(f"  1  →  {LABELS['1']}")
    print(f"  2  →  {LABELS['2']}")
    print(f"  3  →  {LABELS['3']}")
    print("  q  →  Quit")
    print("=" * 42 + "\n")

    # Save current terminal settings before modifying them
    old_settings = termios.tcgetattr(sys.stdin)
    
    try:
        # Set terminal to raw mode so it reacts immediately to a single keypress
        # without waiting for the user to press Enter.
        tty.setraw(sys.stdin.fileno())
        
        while True:
            # Read exactly one character
            k = sys.stdin.read(1)
            
            # Use \r\n instead of \n because raw mode bypasses the standard
            # terminal carriage-return behavior.
            if k == "1":
                print(f"\r  {LABELS['1']}\r\n", end="")
                play(sound_antilag_snap())
            elif k == "2":
                print(f"\r  {LABELS['2']}\r\n", end="")
                play(sound_decel_burble())
            elif k == "3":
                print(f"\r  {LABELS['3']}\r\n", end="")
                play(sound_soft_snap())
            elif k.lower() == "q" or k == "\x03": # \x03 is Ctrl+C
                print("\r\nBye!\r\n", end="")
                break
            elif k.isprintable():
                print(f"\r{k}\r\n", end="")
                
    finally:
        # Guarantee normal terminal settings are restored when the script exits
        # or if an unexpected crash occurs.
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


if __name__ == "__main__":
    main()
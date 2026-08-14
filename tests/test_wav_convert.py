"""WAV -> mu-law conversion tests — run with: python tests/test_wav_convert.py

Covers the anti-aliasing fix in groq_tts.py: audioop.ratecv used to
resample Orpheus 24kHz WAV to 8kHz with no low-pass filter, folding every
component above 4kHz back into the voice band (audible static on narrowband
phone lines). The replacement (resample_poly) must keep a 1kHz tone intact
while removing a 6kHz tone entirely — the exact aliasing failure mode.
"""

import audioop
import io
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.groq_tts import TARGET_RATE, _wav_to_mulaw_8k, resample_to_8k

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(name)
    print(f"  [{status}] {name}{' — ' + detail if detail else ''}")


def make_wav(rate: int, pcm: np.ndarray, channels: int = 1) -> bytes:
    """Wraps int16 PCM into a WAV blob in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        if channels == 2:
            stereo = np.repeat(pcm.reshape(-1, 1), 2, axis=1).astype(np.int16)
            wf.writeframes(stereo.tobytes())
        else:
            wf.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


def tone(rate: int, freq: float, dur_s: float, amplitude: float) -> np.ndarray:
    n = int(rate * dur_s)
    t = np.arange(n) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def decode_mulaw(mulaw: bytes) -> np.ndarray:
    return np.frombuffer(audioop.ulaw2lin(mulaw, 2), dtype=np.int16)


def dominant_freq(lin: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(lin))
    bin_idx = int(np.argmax(spectrum))
    return bin_idx * (rate / len(lin))


def rms(lin: np.ndarray) -> float:
    if len(lin) == 0:
        return 0.0
    return float(np.sqrt(np.mean(lin.astype(np.float64) ** 2)))


def test_anti_aliasing() -> None:
    print("=" * 60)
    print("TEST 1: anti-aliased 24k->8k keeps 1kHz, removes 6kHz")
    print("=" * 60)

    in_rate = 24000
    in_tone_1k = tone(in_rate, 1000, 0.25, 6000)
    in_tone_6k = tone(in_rate, 6000, 0.25, 6000)
    in_rms = rms(in_tone_1k)

    out_1k = decode_mulaw(_wav_to_mulaw_8k(make_wav(in_rate, in_tone_1k)))
    out_6k = decode_mulaw(_wav_to_mulaw_8k(make_wav(in_rate, in_tone_6k)))

    freq_1k = dominant_freq(out_1k, TARGET_RATE)
    check(
        "1kHz tone survives the conversion",
        rms(out_1k) > 0.4 * in_rms,
        detail=f"out_rms={rms(out_1k):.0f} vs in_rms={in_rms:.0f}",
    )
    check(
        "1kHz tone stays at ~1kHz (no fold shift)",
        abs(freq_1k - 1000) <= 60,
        detail=f"dominant={freq_1k:.0f}Hz",
    )
    # The regression pin: under the old audioop.ratecv path this 6kHz tone
    # aliased into the 0-4kHz band as a loud spurious tone. The FIR filter
    # must remove it, leaving only numerical noise.
    check(
        "6kHz tone produces ~no in-band energy",
        rms(out_6k) < 0.02 * in_rms,
        detail=f"out_rms={rms(out_6k):.0f} vs in_rms={in_rms:.0f}",
    )


def test_8k_passthrough() -> None:
    print("=" * 60)
    print("TEST 2: 8kHz input passes through unchanged")
    print("=" * 60)

    pcm = tone(8000, 1000, 0.25, 6000)
    wav = make_wav(8000, pcm)
    expected = audioop.lin2ulaw(pcm.tobytes(), 2)
    check(
        "_wav_to_mulaw_8k skips resampling at 8kHz",
        _wav_to_mulaw_8k(wav) == expected,
    )
    check(
        "resample_to_8k returns the same bytes at orig_rate==8000",
        resample_to_8k(pcm.tobytes(), 8000) == pcm.tobytes(),
    )


def test_stereo_integration() -> None:
    print("=" * 60)
    print("TEST 3: stereo 24kHz == mono 24kHz after conversion (tomono + resample)")
    print("=" * 60)

    in_rate = 24000
    mono = tone(in_rate, 1000, 0.25, 6000)
    out_mono = _wav_to_mulaw_8k(make_wav(in_rate, mono, channels=1))
    out_stereo = _wav_to_mulaw_8k(make_wav(in_rate, mono, channels=2))
    check(
        "stereo and mono inputs convert to identical output",
        out_mono == out_stereo,
        detail=f"len_mono={len(out_mono)} len_stereo={len(out_stereo)}",
    )


def main() -> None:
    test_anti_aliasing()
    test_8k_passthrough()
    test_stereo_integration()

    print("=" * 60)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S)")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("RESULT: ALL PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()

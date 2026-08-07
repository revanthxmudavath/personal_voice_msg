from __future__ import annotations

import numpy as np
import pytest

from personal_voice_msg.voice_enrollment import SILENCE_RMS_FLOOR, _trim_leading_silence

pytestmark = pytest.mark.fast


def _tone(seconds: float, sample_rate: int, amplitude: float = 0.2) -> np.ndarray:
    t = np.linspace(0, seconds, int(seconds * sample_rate), endpoint=False)
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype("float32")


def test_leading_near_silence_is_trimmed() -> None:
    sample_rate = 24000
    silence = np.zeros(int(2.5 * sample_rate), dtype="float32")
    speech = _tone(1.0, sample_rate)
    data = np.concatenate([silence, speech])

    trimmed = _trim_leading_silence(data, sample_rate)

    assert len(trimmed) <= len(speech) + int(0.1 * sample_rate)
    assert len(trimmed) >= len(speech) - int(0.1 * sample_rate)


def test_speech_with_no_leading_silence_is_unchanged() -> None:
    sample_rate = 24000
    speech = _tone(2.0, sample_rate)

    trimmed = _trim_leading_silence(speech, sample_rate)

    assert len(trimmed) == len(speech)


def test_fully_silent_input_is_returned_unchanged() -> None:
    sample_rate = 24000
    silence = np.zeros(int(2.0 * sample_rate), dtype="float32")

    trimmed = _trim_leading_silence(silence, sample_rate)

    assert len(trimmed) == len(silence)


def test_trimmed_audio_starts_above_the_silence_floor() -> None:
    sample_rate = 24000
    silence = np.zeros(int(1.0 * sample_rate), dtype="float32")
    speech = _tone(1.0, sample_rate)
    data = np.concatenate([silence, speech])

    trimmed = _trim_leading_silence(data, sample_rate)
    window = trimmed[: int(0.1 * sample_rate)]
    rms = float(np.sqrt(np.mean(np.square(window))))

    assert rms >= SILENCE_RMS_FLOOR

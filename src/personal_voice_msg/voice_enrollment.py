from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from pocket_tts import TTSModel, export_model_state
from safetensors.torch import load_file

MIN_ENROLLMENT_SECONDS = 3.0
SILENCE_RMS_FLOOR = 0.001
CLIPPING_SAMPLE_THRESHOLD = 0.999
CLIPPING_RATIO_CEILING = 0.01
LEADING_SILENCE_WINDOW_SECONDS = 0.1


class EnrollmentError(ValueError):
    """Raised when an enrollment audio sample fails validation."""


def _trim_leading_silence(
    data: np.ndarray, sample_rate: int, *, rms_floor: float = SILENCE_RMS_FLOOR
) -> np.ndarray:
    """Skip a leading near-silent lead-in (e.g. a pause before someone starts
    talking into a voice memo recorder) so enrollment's 30-second
    voice-conditioning truncation spends its budget on real speech instead
    of dead air. Returns ``data`` unchanged if it never rises above the
    silence floor -- ``validate_sample`` rejects fully silent input anyway.
    """

    window = max(1, int(LEADING_SILENCE_WINDOW_SECONDS * sample_rate))
    for start in range(0, len(data), window):
        chunk = data[start : start + window]
        if chunk.size == 0:
            break
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        if rms >= rms_floor:
            return data[start:]
    return data


def validate_sample(path: Path) -> None:
    try:
        data, sample_rate = sf.read(str(path), dtype="float32")
    except sf.SoundFileError:
        raise EnrollmentError(
            f"unsupported or unreadable audio format: {path}"
        ) from None

    duration_seconds = len(data) / sample_rate
    if duration_seconds < MIN_ENROLLMENT_SECONDS:
        raise EnrollmentError(
            f"sample is too short: {duration_seconds:.2f}s "
            f"(minimum {MIN_ENROLLMENT_SECONDS:.2f}s)"
        )

    rms = float(np.sqrt(np.mean(np.square(data))))
    if rms < SILENCE_RMS_FLOOR:
        raise EnrollmentError(f"sample is silent: RMS {rms:.6f}")

    clipping_ratio = float(np.mean(np.abs(data) >= CLIPPING_SAMPLE_THRESHOLD))
    if clipping_ratio > CLIPPING_RATIO_CEILING:
        raise EnrollmentError(f"sample is clipped: {clipping_ratio:.2%} of samples")


def enroll_voice(
    sample_path: Path,
    destination: Path,
    *,
    model: TTSModel | None = None,
) -> Path:
    """Validate, embed, verify, and export a consented enrollment sample.

    Trims a leading near-silent lead-in first, then conditions on at most
    the first 30 seconds of the result (Pocket TTS's own recommended limit
    for voice-conditioning prompts) -- an untruncated longer sample
    destabilizes end-of-speech detection during later synthesis, and a
    leading pause before speech starts otherwise burns part of that 30s
    budget on dead air instead of usable voice (both found while
    calibrating T14's audio pipeline; see docs/task-logs/T14.md).

    Deletes ``sample_path`` only after the exported embedding at
    ``destination`` has been read back and verified.
    """

    validate_sample(sample_path)

    if model is None:
        model = TTSModel.load_model()

    data, sample_rate = sf.read(str(sample_path), dtype="float32")
    trimmed = _trim_leading_silence(data, sample_rate)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        trimmed_path = Path(handle.name)
    try:
        sf.write(str(trimmed_path), trimmed, sample_rate)
        state = model.get_state_for_audio_prompt(trimmed_path, truncate=True)
    finally:
        trimmed_path.unlink(missing_ok=True)
    export_model_state(state, destination)

    exported = load_file(str(destination))
    if not exported or not all(tensor.numel() > 0 for tensor in exported.values()):
        raise EnrollmentError(
            f"exported embedding at {destination} failed verification"
        )

    sample_path.unlink()

    return destination

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from pocket_tts import TTSModel, export_model_state
from safetensors.torch import load_file

MIN_ENROLLMENT_SECONDS = 3.0
SILENCE_RMS_FLOOR = 0.001
CLIPPING_SAMPLE_THRESHOLD = 0.999
CLIPPING_RATIO_CEILING = 0.01


class EnrollmentError(ValueError):
    """Raised when an enrollment audio sample fails validation."""


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

    Deletes ``sample_path`` only after the exported embedding at
    ``destination`` has been read back and verified.
    """

    validate_sample(sample_path)

    if model is None:
        model = TTSModel.load_model()

    state = model.get_state_for_audio_prompt(sample_path)
    export_model_state(state, destination)

    exported = load_file(str(destination))
    if not exported or not all(tensor.numel() > 0 for tensor in exported.values()):
        raise EnrollmentError(
            f"exported embedding at {destination} failed verification"
        )

    sample_path.unlink()

    return destination

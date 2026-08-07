from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from safetensors.torch import load_file

from personal_voice_msg.voice_enrollment import (
    MIN_ENROLLMENT_SECONDS,
    EnrollmentError,
    enroll_voice,
    validate_sample,
)

pytestmark = pytest.mark.integration

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
UNSUPPORTED_FORMAT_SAMPLE_ENV = "T13_VOICE_SAMPLE_UNSUPPORTED_FORMAT"

if VOICE_SAMPLE_ENV not in os.environ:
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample; "
                f"set {VOICE_SAMPLE_ENV} to a WAV file path"
            )
        ),
    ]


def _base_sample_path() -> Path:
    return Path(os.environ[VOICE_SAMPLE_ENV])


def _read_base_sample() -> tuple[np.ndarray, int]:
    data, sample_rate = sf.read(str(_base_sample_path()), dtype="float32")
    return data, sample_rate


def test_unsupported_format_sample_is_rejected() -> None:
    unsupported_path = os.environ.get(UNSUPPORTED_FORMAT_SAMPLE_ENV)
    if not unsupported_path:
        pytest.skip(
            "requires a real consented sample in an unsupported container; "
            f"set {UNSUPPORTED_FORMAT_SAMPLE_ENV}"
        )

    with pytest.raises(EnrollmentError, match="unsupported"):
        validate_sample(Path(unsupported_path))


def test_too_short_sample_is_rejected(tmp_path: Path) -> None:
    data, sample_rate = _read_base_sample()
    too_short = data[: int(0.5 * sample_rate)]
    sample_path = tmp_path / "too_short.wav"
    sf.write(str(sample_path), too_short, sample_rate)

    with pytest.raises(EnrollmentError, match="too short"):
        validate_sample(sample_path)


def test_silent_sample_is_rejected(tmp_path: Path) -> None:
    _, sample_rate = _read_base_sample()
    silence_samples = int(MIN_ENROLLMENT_SECONDS * sample_rate) + sample_rate
    silence = np.zeros(silence_samples, dtype="float32")
    sample_path = tmp_path / "silent.wav"
    sf.write(str(sample_path), silence, sample_rate)

    with pytest.raises(EnrollmentError, match="silent"):
        validate_sample(sample_path)


def test_clipped_sample_is_rejected(tmp_path: Path) -> None:
    data, sample_rate = _read_base_sample()
    clipped = np.clip(data * 20.0, -1.0, 1.0)
    sample_path = tmp_path / "clipped.wav"
    sf.write(str(sample_path), clipped, sample_rate)

    with pytest.raises(EnrollmentError, match="clipped"):
        validate_sample(sample_path)


@pytest.fixture(scope="module")
def enrollment_result(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Run real enrollment once: real Pocket TTS, real filesystem, no mocks."""

    workdir = tmp_path_factory.mktemp("t13_enrollment")
    raw_sample = workdir / "raw_sample.wav"
    shutil.copyfile(_base_sample_path(), raw_sample)
    destination = workdir / "voice_embedding.safetensors"

    enroll_voice(raw_sample, destination)

    return destination, raw_sample


def test_enroll_voice_produces_usable_embedding(
    enrollment_result: tuple[Path, Path],
) -> None:
    destination, _raw_sample = enrollment_result

    assert destination.is_file()
    tensors = load_file(str(destination))
    assert tensors
    assert all(tensor.numel() > 0 for tensor in tensors.values())


def test_enroll_voice_deletes_raw_sample_after_export(
    enrollment_result: tuple[Path, Path],
) -> None:
    _destination, raw_sample = enrollment_result

    assert not raw_sample.exists()

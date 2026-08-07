from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from pocket_tts import TTSModel

from personal_voice_msg.database import Database, MessageState

TARGET_SAMPLE_RATE = 48000
TARGET_CHANNELS = 1
OPUS_BITRATE = "24k"
MAX_AUDIO_DURATION_SECONDS = 60.0
SILENCE_RMS_FLOOR = 0.001
CLIPPING_SAMPLE_THRESHOLD = 0.999
CLIPPING_RATIO_CEILING = 0.01
# Pocket TTS always synthesizes at this rate (TTSModel.sample_rate). Ogg
# Opus containers always report the fixed 48000 Hz decode rate regardless of
# encoding settings (RFC 7845), so a wrong-sample-rate source WAV can only be
# caught before conversion, not by probing the converted output.
EXPECTED_SOURCE_SAMPLE_RATE = 24000

# Pocket TTS ships DEFAULT_EOS_THRESHOLD = -4.0, which detects end-of-speech
# almost immediately when conditioned on a cloned voice (confirmed by
# tracing into TTSModel._autoregressive_generation: eos fires within the
# first few generation steps, producing well under a second of audio for a
# multi-word sentence). A calibration sweep across multiple sentences and
# two different voice embeddings found eos_threshold=0.0 with
# frames_after_eos=10 gives consistent, natural word rates (~2-3 words/sec)
# with no generation reaching its length ceiling, for both the cloned test
# voice and a reference built-in voice.
EOS_THRESHOLD = 0.0
FRAMES_AFTER_EOS = 10


class AudioPipelineError(RuntimeError):
    """Raised when speech synthesis, conversion, or validation fails."""


def synthesize_to_wav(
    embedding_path: Path,
    text: str,
    destination: Path,
    *,
    model: TTSModel | None = None,
) -> Path:
    """Synthesize ``text`` in the enrolled voice and write it as a WAV file."""

    if model is None:
        model = TTSModel.load_model()
    model.eos_threshold = EOS_THRESHOLD

    try:
        voice_state = model.get_state_for_audio_prompt(embedding_path)
        audio = model.generate_audio(
            voice_state, text, frames_after_eos=FRAMES_AFTER_EOS
        )
    except Exception as error:
        raise AudioPipelineError(f"speech synthesis failed: {error}") from error

    samples = audio.detach().cpu().numpy()
    if samples.ndim > 1:
        samples = samples.squeeze(0)
    sf.write(str(destination), samples, model.sample_rate)
    return destination


def convert_to_opus(source_wav: Path, destination: Path) -> Path:
    """Convert a WAV file to a WhatsApp-compatible OGG/Opus file via FFmpeg."""

    source_rate = int(sf.info(str(source_wav)).samplerate)
    if source_rate != EXPECTED_SOURCE_SAMPLE_RATE:
        raise AudioPipelineError(
            f"source audio has the wrong sample rate: {source_rate} "
            f"(expected {EXPECTED_SOURCE_SAMPLE_RATE})"
        )

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_wav),
            "-c:a",
            "libopus",
            "-b:a",
            OPUS_BITRATE,
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-ac",
            str(TARGET_CHANNELS),
            "-application",
            "voip",
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise AudioPipelineError(f"FFmpeg conversion failed: {result.stderr.strip()}")
    return destination


def _probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate:format=format_name,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioPipelineError(f"audio is corrupt: {result.stderr.strip()}")
    try:
        probe: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AudioPipelineError("audio is corrupt: bad ffprobe output") from error
    if not probe.get("streams") or not probe.get("format"):
        raise AudioPipelineError("audio is corrupt: no stream or format data")
    return probe


def validate_audio(path: Path) -> None:
    """Probe format, signal, and duration; raise on any WhatsApp-readiness failure."""

    probe = _probe(path)
    stream = probe["streams"][0]
    format_info = probe["format"]

    codec = stream.get("codec_name")
    format_name = format_info.get("format_name", "")
    if codec != "opus" or "ogg" not in format_name:
        raise AudioPipelineError(
            f"audio is not OGG/Opus: codec={codec}, format={format_name}"
        )

    duration = float(format_info.get("duration", 0.0))
    if duration > MAX_AUDIO_DURATION_SECONDS:
        raise AudioPipelineError(
            f"audio exceeds the maximum duration: {duration:.2f}s "
            f"(maximum {MAX_AUDIO_DURATION_SECONDS:.2f}s)"
        )

    data, _ = sf.read(str(path), dtype="float32")
    rms = float(np.sqrt(np.mean(np.square(data))))
    if rms < SILENCE_RMS_FLOOR:
        raise AudioPipelineError(f"audio is silent: RMS {rms:.6f}")

    clipping_ratio = float(np.mean(np.abs(data) >= CLIPPING_SAMPLE_THRESHOLD))
    if clipping_ratio > CLIPPING_RATIO_CEILING:
        raise AudioPipelineError(f"audio is clipped: {clipping_ratio:.2%} of samples")


def produce_voice_note(
    database: Database,
    delivery_id: int,
    embedding_path: Path,
    text: str,
    destination: Path,
    now: datetime,
    *,
    model: TTSModel | None = None,
) -> Path:
    """Synthesize, convert, and validate a voice note; mark it ``audio_ready``.

    Leaves no file at ``destination`` and no delivery-state change on any
    failure, so a failed synthesis never leaves a sendable artifact behind.
    """

    temp_wav = destination.with_suffix(".wav")
    try:
        synthesize_to_wav(embedding_path, text, temp_wav, model=model)
        convert_to_opus(temp_wav, destination)
        validate_audio(destination)
    except Exception:
        temp_wav.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    else:
        temp_wav.unlink(missing_ok=True)

    database.transition_delivery(delivery_id, MessageState.AUDIO_READY, now)
    return destination


def remove_audio_after_delivery(
    database: Database,
    delivery_id: int,
    audio_path: Path,
) -> None:
    """Delete the temporary voice-note file, but only once delivery is sent."""

    state = database.get_delivery_state(delivery_id)
    if state is not MessageState.SENT:
        raise AudioPipelineError(
            f"delivery is not sent (state={state.value}); refusing to remove audio"
        )
    audio_path.unlink(missing_ok=True)

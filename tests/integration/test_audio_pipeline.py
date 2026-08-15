from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pocket_tts import TTSModel

from personal_voice_msg.audio_pipeline import (
    MAX_AUDIO_DURATION_SECONDS,
    TARGET_SAMPLE_RATE,
    AudioPipelineError,
    convert_to_opus,
    produce_voice_note,
    remove_audio_after_delivery,
    synthesize_to_wav,
    validate_audio,
)
from personal_voice_msg.database import Database, DatabaseInvariantError, MessageState
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.voice_enrollment import enroll_voice

pytestmark = pytest.mark.integration

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
RECIPIENT = "recipient_t14_test"

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


def _ffprobe_json(path: Path) -> dict:
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
    return json.loads(result.stdout)


def record_and_reserve(database: Database, text: str) -> int:
    """Real DB path from DISCOVERED to a RESERVED delivery. No mocks."""

    database.migrate()
    decision = MessageHistory(database).evaluate_and_record(text, NOW)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, NOW)
    reservation = database.reserve_next_message(RECIPIENT, date(2026, 8, 7), NOW)
    assert reservation is not None
    return reservation.delivery_id


@pytest.fixture(scope="module")
def model() -> TTSModel:
    return TTSModel.load_model()


@pytest.fixture(scope="module")
def voice_embedding(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real Pocket TTS embedding, enrolled once from the consented test voice."""

    workdir = tmp_path_factory.mktemp("t14_embedding")
    raw_sample = workdir / "raw_sample.wav"
    import shutil

    shutil.copyfile(_base_sample_path(), raw_sample)
    destination = workdir / "voice_embedding.safetensors"
    enroll_voice(raw_sample, destination)
    return destination


@pytest.fixture(scope="module")
def synthesized_wav(
    tmp_path_factory: pytest.TempPathFactory, model: TTSModel, voice_embedding: Path
) -> Path:
    """Real Pocket TTS synthesis, once, reused across happy-path tests."""

    workdir = tmp_path_factory.mktemp("t14_synth")
    destination = workdir / "synthesized.wav"
    synthesize_to_wav(
        voice_embedding,
        "I hope your day is full of warmth and good coffee.",
        destination,
        model=model,
    )
    return destination


def _write_wav(path: Path, data: np.ndarray, sample_rate: int) -> Path:
    sf.write(str(path), data, sample_rate)
    return path


# Pocket TTS's own shipped default (eos_threshold=-4.0) detects end-of-speech
# almost immediately when conditioned on a cloned voice, producing well
# under a second of audio for a multi-word sentence -- confirmed by tracing
# into the library's real generation loop, not guessed. A real spoken
# sentence at a natural pace runs well under 4 words/second; this floor
# would have failed against the unpatched default (~0.4-0.8s for these
# sentences) and passes once synthesize_to_wav uses a calibrated threshold.
MIN_PLAUSIBLE_WORDS_PER_SECOND = 4.0


def test_synthesized_audio_is_not_truncated_by_premature_eos(
    tmp_path: Path, model: TTSModel, voice_embedding: Path
) -> None:
    text = "Your smile is honestly the best part of my week."
    word_count = len(text.split())
    destination = tmp_path / "not_truncated.wav"

    synthesize_to_wav(voice_embedding, text, destination, model=model)

    data, sample_rate = sf.read(str(destination), dtype="float32")
    duration_seconds = len(data) / sample_rate

    assert duration_seconds >= word_count / MIN_PLAUSIBLE_WORDS_PER_SECOND, (
        f"{duration_seconds:.2f}s is too short for a {word_count}-word "
        "sentence spoken at a natural pace -- audio was likely truncated "
        "by premature end-of-speech detection"
    )


# An untruncated enrollment sample longer than Pocket TTS's own recommended
# 30s voice-conditioning limit destabilizes EOS detection for longer
# generated sentences specifically -- short sentences (the case above) were
# unaffected, which is why this needs its own, longer-text test. Confirmed
# by a real differential: the same eos_threshold behaved consistently for a
# built-in reference voice on the same sentences, and truncating the
# conditioning to 30s (get_state_for_audio_prompt(..., truncate=True))
# fixed it for the cloned voice too.
MAX_PLAUSIBLE_WORDS_PER_SECOND = 4.5
MIN_PLAUSIBLE_WORDS_PER_SECOND_LONG = 1.5


def test_synthesized_audio_is_stable_for_longer_sentences(
    tmp_path: Path,
    model: TTSModel,
    voice_embedding: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    text = (
        "I hope your morning starts with warm sunshine, a little extra "
        "coffee, and something small to smile about."
    )
    word_count = len(text.split())
    destination = tmp_path / "long_sentence.wav"

    with caplog.at_level("WARNING"):
        synthesize_to_wav(voice_embedding, text, destination, model=model)

    assert "Maximum generation length reached without EOS" not in caplog.text, (
        "generation hit its length ceiling without finding a natural end "
        "-- the voice-conditioning prompt is likely destabilizing EOS "
        "detection for longer text"
    )

    data, sample_rate = sf.read(str(destination), dtype="float32")
    duration_seconds = len(data) / sample_rate
    rate = word_count / duration_seconds
    lower, upper = MIN_PLAUSIBLE_WORDS_PER_SECOND_LONG, MAX_PLAUSIBLE_WORDS_PER_SECOND
    assert lower <= rate <= upper, (
        f"{duration_seconds:.2f}s for a {word_count}-word sentence is "
        f"{rate:.2f} words/sec, outside a natural speaking-pace range"
    )


# ---------------------------------------------------------------------------
# validate_audio: signal- and format-boundary rejections, derived from the
# one real consented recording (same technique as T13).
# ---------------------------------------------------------------------------


def test_silent_audio_is_rejected(tmp_path: Path) -> None:
    _, sample_rate = _read_base_sample()
    silence = np.zeros(int(3.0 * sample_rate), dtype="float32")
    wav_path = _write_wav(tmp_path / "silent.wav", silence, sample_rate)
    ogg_path = tmp_path / "silent.ogg"
    convert_to_opus(wav_path, ogg_path)

    with pytest.raises(AudioPipelineError, match="silent"):
        validate_audio(ogg_path)


def test_clipped_audio_is_rejected(tmp_path: Path) -> None:
    data, sample_rate = _read_base_sample()
    clipped = np.clip(data * 20.0, -1.0, 1.0)
    wav_path = _write_wav(tmp_path / "clipped.wav", clipped, sample_rate)
    ogg_path = tmp_path / "clipped.ogg"
    convert_to_opus(wav_path, ogg_path)

    with pytest.raises(AudioPipelineError, match="clipped"):
        validate_audio(ogg_path)


def test_corrupt_audio_is_rejected(tmp_path: Path) -> None:
    data, sample_rate = _read_base_sample()
    wav_path = _write_wav(tmp_path / "valid.wav", data, sample_rate)
    ogg_path = tmp_path / "valid.ogg"
    convert_to_opus(wav_path, ogg_path)

    corrupt_path = tmp_path / "corrupt.ogg"
    valid_bytes = ogg_path.read_bytes()
    # A few hundred bytes cuts mid-page, well below any valid file's header
    # structure regardless of duration -- ffprobe genuinely fails to parse
    # this ("End of file"), unlike a proportional truncation which can still
    # leave enough complete Ogg pages for ffprobe to read successfully.
    corrupt_path.write_bytes(valid_bytes[:200])

    with pytest.raises(AudioPipelineError, match="corrupt"):
        validate_audio(corrupt_path)


def test_incorrectly_sampled_audio_is_rejected(tmp_path: Path) -> None:
    # Ogg Opus containers always report the fixed 48000 Hz decode rate
    # (RFC 7845), regardless of encoding parameters -- confirmed by directly
    # inspecting ffprobe's output for a file encoded with -ar 8000. A wrong
    # source sample rate can only be caught before conversion, on the
    # pre-encode WAV, which is what convert_to_opus checks.
    data, sample_rate = _read_base_sample()
    wrong_rate_wav = _write_wav(tmp_path / "wrong_rate.wav", data, 8000)
    destination = tmp_path / "wrong_rate.ogg"

    with pytest.raises(AudioPipelineError, match="sample rate"):
        convert_to_opus(wrong_rate_wav, destination)

    assert not destination.exists()


def test_excessive_duration_audio_is_rejected(tmp_path: Path) -> None:
    data, sample_rate = _read_base_sample()
    repeats = int(MAX_AUDIO_DURATION_SECONDS // (len(data) / sample_rate)) + 2
    long_data = np.tile(data, repeats)
    wav_path = _write_wav(tmp_path / "long.wav", long_data, sample_rate)
    ogg_path = tmp_path / "long.ogg"
    convert_to_opus(wav_path, ogg_path)

    with pytest.raises(AudioPipelineError, match="duration"):
        validate_audio(ogg_path)


def test_valid_voice_note_decodes_as_ogg_opus(
    tmp_path: Path, synthesized_wav: Path
) -> None:
    ogg_path = tmp_path / "voice_note.ogg"
    convert_to_opus(synthesized_wav, ogg_path)
    validate_audio(ogg_path)

    probe = _ffprobe_json(ogg_path)
    assert "ogg" in probe["format"]["format_name"]
    assert probe["streams"][0]["codec_name"] == "opus"
    assert int(probe["streams"][0]["sample_rate"]) == TARGET_SAMPLE_RATE


# ---------------------------------------------------------------------------
# produce_voice_note: orchestration, failure cleanup, delivery-state wiring.
# ---------------------------------------------------------------------------


def test_produce_voice_note_persists_audio_and_marks_ready(
    tmp_path: Path, model: TTSModel, voice_embedding: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = record_and_reserve(database, "Thinking of you and smiling today.")
    destination = tmp_path / "note.ogg"

    result = produce_voice_note(
        database,
        delivery_id,
        voice_embedding,
        "Thinking of you and smiling today.",
        destination,
        NOW,
        model=model,
    )

    assert isinstance(result, bytes) and result
    assert not destination.exists()
    assert database.get_delivery_state(delivery_id) == MessageState.AUDIO_READY
    assert database.get_audio_data(delivery_id) == result


def test_failed_synthesis_leaves_no_sendable_artifact(
    tmp_path: Path, model: TTSModel
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    text = "A message that will fail to synthesize."
    delivery_id = record_and_reserve(database, text)
    destination = tmp_path / "note.ogg"
    bogus_embedding = tmp_path / "does_not_exist.safetensors"

    with pytest.raises(AudioPipelineError):
        produce_voice_note(
            database,
            delivery_id,
            bogus_embedding,
            text,
            destination,
            NOW,
            model=model,
        )

    assert not destination.exists()
    assert database.get_delivery_state(delivery_id) == MessageState.RESERVED


def test_successful_delivery_clears_stored_audio(
    tmp_path: Path, model: TTSModel, voice_embedding: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    delivery_id = record_and_reserve(database, "A sentence sent all the way through.")
    destination = tmp_path / "note.ogg"
    produce_voice_note(
        database,
        delivery_id,
        voice_embedding,
        "A sentence sent all the way through.",
        destination,
        NOW,
        model=model,
    )
    database.transition_delivery(delivery_id, MessageState.SENDING, NOW)
    database.record_delivery_attempt(delivery_id, MessageState.SENT, NOW)

    remove_audio_after_delivery(database, delivery_id, NOW)

    with pytest.raises(DatabaseInvariantError):
        database.get_audio_data(delivery_id)


def test_remove_audio_before_delivery_confirmed_is_refused(tmp_path: Path) -> None:
    delivery_id = record_and_reserve(
        Database(tmp_path / "state.sqlite3"), "Not sent yet, do not delete me."
    )
    database = Database(tmp_path / "state.sqlite3")
    database.mark_audio_ready(delivery_id, b"placeholder", NOW)

    with pytest.raises(AudioPipelineError, match="not.*sent"):
        remove_audio_after_delivery(database, delivery_id, NOW)

    assert database.get_audio_data(delivery_id) == b"placeholder"

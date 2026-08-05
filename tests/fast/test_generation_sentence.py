from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_voice_msg.discovery.inspiration import (
    Emotion,
    Imagery,
    InspirationCard,
    RightsCategory,
    Theme,
    Tone,
)
from personal_voice_msg.generation.sentence import (
    SentenceValidationError,
    build_prompt,
    validate_generated_sentence,
)

CARD = InspirationCard(
    theme=Theme.APPRECIATION,
    emotion=Emotion.WARMTH,
    imagery=Imagery.MORNING_LIGHT,
    tone=Tone.GENTLE,
    source="https://example.invalid/poem",
    rights_category=RightsCategory.UNKNOWN,
    evidence="unused in this test",
    discovery_timestamp=datetime(2026, 8, 4, tzinfo=UTC),
)


@pytest.mark.fast
def test_prompt_includes_all_four_semantic_signals_and_excludes_source() -> None:
    prompt = build_prompt(CARD)

    assert "appreciation" in prompt
    assert "warmth" in prompt
    assert "morning light" in prompt
    assert "gentle" in prompt
    assert "example.invalid" not in prompt
    assert "unused in this test" not in prompt


@pytest.mark.fast
def test_accepts_a_single_clean_sentence() -> None:
    result = validate_generated_sentence(
        "Your gentle heart has a wonderful way of making every day feel brighter."
    )
    assert result == (
        "Your gentle heart has a wonderful way of making every day feel brighter."
    )


@pytest.mark.fast
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Two sentences. Right here.",
        "No terminal punctuation at all",
        "Visit https://example.com for more.",
        "Check out www.example.com today.",
    ],
    ids=[
        "empty",
        "whitespace-only",
        "multi-sentence",
        "no-terminal-punctuation",
        "https-url",
        "www-url",
    ],
)
def test_rejects_structurally_invalid_output(raw: str) -> None:
    with pytest.raises(SentenceValidationError):
        validate_generated_sentence(raw)


@pytest.mark.fast
def test_rejects_six_consecutive_source_words() -> None:
    source = "The moon keeps a silver promise above the quiet sleeping city"
    candidate = (
        "Tonight, a silver promise above the quiet sleeping city makes me smile."
    )

    with pytest.raises(SentenceValidationError):
        validate_generated_sentence(candidate, source_text=source)

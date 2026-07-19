from __future__ import annotations

import pytest

from personal_voice_msg.normalization import (
    copies_source_span,
    normalize_text,
    normalized_hash,
)


@pytest.mark.fast
@pytest.mark.parametrize(
    "variant",
    [
        "you are my favorite person",
        "YOU ARE MY FAVORITE PERSON",
        "  You\tare\nmy   favorite person  ",
        "You—are; my... favorite, person!",
        "Ｙｏｕ are my favorite person",
    ],
    ids=["exact", "case", "whitespace", "punctuation", "unicode"],
)
def test_equivalent_text_normalizes_to_the_same_value(variant: str) -> None:
    assert normalize_text(variant) == "you are my favorite person"


@pytest.mark.fast
def test_unicode_combining_forms_normalize_equally() -> None:
    assert normalize_text("Café moments") == normalize_text("Cafe\u0301 moments")


@pytest.mark.fast
def test_equivalent_text_produces_the_same_stable_hash() -> None:
    variants = [
        "You are my favorite person.",
        "YOU ARE MY FAVORITE PERSON",
        "  you\tare my favorite person!  ",
        "Ｙｏｕ are my favorite person",
    ]

    hashes = {normalized_hash(variant) for variant in variants}

    assert len(hashes) == 1
    assert hashes != {normalized_hash("You make every ordinary day brighter")}


@pytest.mark.fast
def test_six_consecutive_source_words_are_detected() -> None:
    source = "At dusk, your smile feels like a small sunrise beside me."
    candidate = "Tonight your smile feels like a small constellation."

    assert copies_source_span(candidate, source, span_words=6)


@pytest.mark.fast
def test_five_consecutive_source_words_are_not_detected() -> None:
    source = "At dusk, your smile feels like a small sunrise beside me."
    candidate = "Tonight your smile feels like a constellation."

    assert not copies_source_span(candidate, source, span_words=6)


@pytest.mark.fast
def test_case_and_punctuation_cannot_hide_a_six_word_source_span() -> None:
    source = "The quiet moon reminds me how your kindness feels like home."
    candidate = "HOW—YOUR, kindness... FEELS; LIKE HOME every day."

    assert copies_source_span(candidate, source, span_words=6)


@pytest.mark.fast
def test_zero_width_format_character_cannot_hide_a_source_span() -> None:
    source = "Your kindness feels like home every day."
    candidate = "Your kind\u200bness feels like home every day."

    assert copies_source_span(candidate, source, span_words=6)

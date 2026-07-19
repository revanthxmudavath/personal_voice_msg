from __future__ import annotations

import hashlib
import unicodedata


def normalize_text(text: str) -> str:
    """Return a stable form for comparison without retaining another text copy."""
    compatible = unicodedata.normalize("NFKC", text).casefold()
    comparable_characters: list[str] = []
    for character in compatible:
        category = unicodedata.category(character)
        if category.startswith("C"):
            if character.isspace():
                comparable_characters.append(" ")
            continue
        comparable_characters.append(" " if category.startswith("P") else character)
    without_punctuation = "".join(comparable_characters)
    return " ".join(without_punctuation.split())


def normalized_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def copies_source_span(
    candidate: str,
    source: str,
    *,
    span_words: int = 6,
) -> bool:
    if span_words < 1:
        raise ValueError("span_words must be positive")

    candidate_words = normalize_text(candidate).split()
    source_words = normalize_text(source).split()
    if len(candidate_words) < span_words or len(source_words) < span_words:
        return False

    source_spans = {
        tuple(source_words[index : index + span_words])
        for index in range(len(source_words) - span_words + 1)
    }
    return any(
        tuple(candidate_words[index : index + span_words]) in source_spans
        for index in range(len(candidate_words) - span_words + 1)
    )

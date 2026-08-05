from __future__ import annotations

import re
from datetime import datetime

import aiohttp

from personal_voice_msg.discovery.inspiration import InspirationCard
from personal_voice_msg.generation.gemini_client import (
    GeminiClientError,
    GeminiGenerationConfig,
    generate_structured,
)
from personal_voice_msg.history import DuplicateDecision, MessageHistory
from personal_voice_msg.normalization import copies_source_span
from personal_voice_msg.redaction import SensitiveValue

MODEL = "gemini-3.6-flash"
TEMPERATURE = 0.2
MAX_OUTPUT_TOKENS = 2048
RESPONSE_SCHEMA: dict[str, object] = {
    "type": "OBJECT",
    "properties": {"sentence": {"type": "STRING"}},
    "required": ["sentence"],
}
_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
_TERMINAL_PUNCTUATION = (".", "!", "?")


class SentenceValidationError(RuntimeError):
    """Report a rejected generated sentence without including its text.

    `rule` is a stable, text-free reason code identifying which check
    rejected the candidate (for example `"empty"`, `"url"`,
    `"client_error"`). `finish_reason` propagates the wrapped
    `GeminiClientError.finish_reason` when the rejection originated from a
    provider call failure, so a `RECITATION`/`SAFETY` block stays
    distinguishable from `MAX_TOKENS` or an ordinary transport error. The
    exception message itself stays generic and never includes candidate
    text.
    """

    def __init__(self, rule: str, *, finish_reason: str | None = None) -> None:
        super().__init__("generated sentence rejected")
        self.rule = rule
        self.finish_reason = finish_reason


def build_prompt(card: InspirationCard) -> str:
    imagery_phrase = card.imagery.value.replace("_", " ")
    return (
        "Write exactly one short, natural, spoken-style English sentence "
        f"expressing {card.theme.value} with a feeling of {card.emotion.value}, "
        f"evoking {imagery_phrase}, in a {card.tone.value} tone. "
        "Do not use any quotes, song lyrics, citations, URLs, names of real "
        "people, or references to a specific shared memory. Do not mention "
        "sex, money, marriage proposals, or breaking up. Return only the "
        "sentence."
    )


def validate_generated_sentence(
    raw: str, *, source_text: str | None = None
) -> str:
    candidate = raw.strip()
    if not candidate:
        raise SentenceValidationError("empty")
    if _URL_PATTERN.search(candidate):
        raise SentenceValidationError("url")
    if candidate[-1] not in _TERMINAL_PUNCTUATION:
        raise SentenceValidationError("no_terminal_punctuation")
    if any(character in _TERMINAL_PUNCTUATION for character in candidate[:-1]):
        raise SentenceValidationError("mid_terminal_punctuation")
    if source_text is not None and copies_source_span(candidate, source_text):
        raise SentenceValidationError("source_copy")
    return candidate


async def generate_sentence(
    session: aiohttp.ClientSession,
    api_key: SensitiveValue[str],
    card: InspirationCard,
    history: MessageHistory,
    now: datetime,
    *,
    source_text: str | None = None,
) -> DuplicateDecision:
    config = GeminiGenerationConfig(
        model=MODEL,
        temperature=TEMPERATURE,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        response_schema=RESPONSE_SCHEMA,
    )
    try:
        structured = await generate_structured(
            session, api_key, build_prompt(card), config
        )
    except GeminiClientError as exc:
        raise SentenceValidationError(
            "client_error", finish_reason=exc.finish_reason
        ) from None
    try:
        sentence = structured["sentence"]
    except (KeyError, TypeError):
        raise SentenceValidationError("malformed_output") from None
    if not isinstance(sentence, str):
        raise SentenceValidationError("malformed_output")

    validated = validate_generated_sentence(sentence, source_text=source_text)
    return history.evaluate_and_record(validated, now, source_text=source_text)

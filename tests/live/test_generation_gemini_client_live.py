from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.generation.gemini_client import (
    GeminiGenerationConfig,
    generate_structured,
)
from personal_voice_msg.redaction import SensitiveValue

if os.environ.get("T10_LIVE_GENERATION") != "1":
    pytestmark = [
        pytest.mark.skip(reason="requires T10_LIVE_GENERATION=1"),
    ]

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"sentence": {"type": "STRING"}},
    "required": ["sentence"],
}


def _real_api_key() -> SensitiveValue[str]:
    key_file = os.environ["GEMINI_API_KEY_FILE"]
    return SensitiveValue(Path(key_file).read_text(encoding="utf-8").strip())


async def _call_returns_a_sentence() -> dict[str, object]:
    config = GeminiGenerationConfig(
        model="gemini-3.6-flash",
        temperature=0.2,
        max_output_tokens=2048,
        response_schema=RESPONSE_SCHEMA,
    )
    async with aiohttp.ClientSession() as session:
        return await generate_structured(
            session,
            _real_api_key(),
            "Write one short, sweet, original sentence about appreciating "
            "someone's kindness. Do not use any quotes, lyrics, URLs, or names.",
            config,
        )


@pytest.mark.live
def test_real_gemini_call_returns_a_sentence() -> None:
    result = asyncio.run(_call_returns_a_sentence())

    assert isinstance(result["sentence"], str)
    assert len(result["sentence"]) > 0


async def _call_with_too_small_a_token_budget() -> None:
    config = GeminiGenerationConfig(
        model="gemini-3.6-flash",
        temperature=0.2,
        max_output_tokens=50,
        response_schema=RESPONSE_SCHEMA,
    )
    async with aiohttp.ClientSession() as session:
        await generate_structured(
            session,
            _real_api_key(),
            "Write one short, sweet, original sentence about appreciating "
            "someone's kindness.",
            config,
        )


@pytest.mark.live
def test_real_gemini_call_rejects_too_small_a_token_budget() -> None:
    with pytest.raises(Exception):
        asyncio.run(_call_with_too_small_a_token_budget())

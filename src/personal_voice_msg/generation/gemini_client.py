from __future__ import annotations

import json
from dataclasses import dataclass

import aiohttp

from personal_voice_msg.redaction import SensitiveValue

GEMINI_API_HOST = "generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"
MAX_RESPONSE_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 30.0


class GeminiClientError(RuntimeError):
    """Report a bounded, non-leaking Gemini API call failure."""


@dataclass(frozen=True, slots=True)
class GeminiGenerationConfig:
    model: str
    temperature: float
    max_output_tokens: int
    response_schema: dict[str, object]


def _parse_generate_content_response(
    payload: dict[str, object],
) -> dict[str, object]:
    try:
        candidates = payload["candidates"]
        first = candidates[0]  # type: ignore[index]
        if first["finishReason"] != "STOP":
            raise GeminiClientError("Gemini generation did not finish cleanly")
        text = first["content"]["parts"][0]["text"]
        structured = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise GeminiClientError("Gemini response was malformed") from None
    if not isinstance(structured, dict):
        raise GeminiClientError("Gemini response was malformed")
    return structured


async def generate_structured(
    session: aiohttp.ClientSession,
    api_key: SensitiveValue[str],
    prompt: str,
    config: GeminiGenerationConfig,
) -> dict[str, object]:
    """Call the real Gemini generateContent boundary and return the parsed reply."""

    url = (
        f"https://{GEMINI_API_HOST}/{GEMINI_API_VERSION}/models/"
        f"{config.model}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": config.temperature,
            "maxOutputTokens": config.max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": config.response_schema,
        },
    }
    try:
        async with session.post(
            url,
            json=body,
            headers={"x-goog-api-key": api_key.reveal()},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise GeminiClientError("Gemini response exceeded the size limit")
            if response.status != 200:
                raise GeminiClientError("Gemini API call failed")
            payload = json.loads(raw)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError):
        raise GeminiClientError("Gemini API call failed") from None

    return _parse_generate_content_response(payload)

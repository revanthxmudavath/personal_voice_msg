from __future__ import annotations

import pytest

from personal_voice_msg.generation.gemini_client import (
    GeminiClientError,
    _parse_generate_content_response,
)

REAL_STOP_RESPONSE = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {
                        "text": (
                            '{"sentence":"Your gentle heart has a wonderful way '
                            'of making every day feel brighter."}'
                        )
                    }
                ],
                "role": "model",
            },
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 28,
        "candidatesTokenCount": 18,
        "totalTokenCount": 557,
        "thoughtsTokenCount": 511,
    },
    "modelVersion": "gemini-3.6-flash",
    "responseId": "cG9yau-CJ_SEz7IPh7rOEQ",
}

REAL_MAX_TOKENS_RESPONSE = {
    "candidates": [
        {
            "content": {"parts": [{"text": "Here is the JSON"}], "role": "model"},
            "finishReason": "MAX_TOKENS",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 28,
        "candidatesTokenCount": 7,
        "totalTokenCount": 224,
        "thoughtsTokenCount": 189,
    },
    "modelVersion": "gemini-3.6-flash",
    "responseId": "Pm9yarihGNKyqtsPj528sQ4",
}


@pytest.mark.fast
def test_parses_real_stop_response_into_structured_dict() -> None:
    result = _parse_generate_content_response(REAL_STOP_RESPONSE)

    assert result == {
        "sentence": (
            "Your gentle heart has a wonderful way of making every day "
            "feel brighter."
        )
    }


@pytest.mark.fast
def test_rejects_max_tokens_finish_reason() -> None:
    with pytest.raises(GeminiClientError):
        _parse_generate_content_response(REAL_MAX_TOKENS_RESPONSE)


@pytest.mark.fast
def test_max_tokens_finish_reason_is_captured_on_the_error() -> None:
    with pytest.raises(GeminiClientError) as excinfo:
        _parse_generate_content_response(REAL_MAX_TOKENS_RESPONSE)

    assert excinfo.value.finish_reason == "MAX_TOKENS"


@pytest.mark.fast
@pytest.mark.parametrize(
    "broken_payload",
    [
        {},
        {"candidates": []},
        {"candidates": [{"finishReason": "STOP"}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]},
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "not valid json"}]},
                }
            ]
        },
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "[1, 2, 3]"}]},
                }
            ]
        },
    ],
    ids=["empty", "no-candidates", "no-content", "no-parts", "bad-json", "not-a-dict"],
)
def test_malformed_response_shapes_fail_closed(broken_payload: dict) -> None:
    with pytest.raises(GeminiClientError):
        _parse_generate_content_response(broken_payload)

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.judging.judge import judge_sentence
from personal_voice_msg.judging.pipeline import evaluate_message_safety
from personal_voice_msg.redaction import SensitiveValue

if os.environ.get("T11_LIVE_JUDGE") != "1":
    pytestmark = [pytest.mark.skip(reason="requires T11_LIVE_JUDGE=1")]


def _real_api_key() -> SensitiveValue[str]:
    key_file = os.environ["GEMINI_API_KEY_FILE"]
    return SensitiveValue(Path(key_file).read_text(encoding="utf-8").strip())


async def _judge(sentence: str) -> object:
    async with aiohttp.ClientSession() as session:
        return await judge_sentence(session, _real_api_key(), sentence)


async def _evaluate(sentence: str) -> object:
    async with aiohttp.ClientSession() as session:
        return await evaluate_message_safety(session, _real_api_key(), sentence)


@pytest.mark.live
def test_real_judge_scores_a_clearly_safe_sentence() -> None:
    result = asyncio.run(
        _judge(
            "Your gentle heart has a wonderful way of making every day "
            "feel brighter."
        )
    )
    assert result.risk_flags == ()
    assert 0.0 <= result.romantic_tone_score <= 10.0
    assert 0.0 <= result.warmth_score <= 10.0
    assert 0.0 <= result.naturalness_score <= 10.0


@pytest.mark.live
def test_real_pipeline_approves_a_clearly_safe_sentence() -> None:
    decision = asyncio.run(
        _evaluate(
            "I hope this quiet morning finds you smiling just like you "
            "make me smile."
        )
    )
    assert decision.approved is True
    assert decision.judge_result is not None
    assert decision.judge_result.risk_flags == ()

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from personal_voice_msg.judging.pipeline import evaluate_message_safety
from personal_voice_msg.redaction import SensitiveValue


async def _evaluate(sentence: str) -> object:
    async with aiohttp.ClientSession() as session:
        return await evaluate_message_safety(
            session, SensitiveValue("not-a-real-api-key"), sentence
        )


# Constructing a real aiohttp.ClientSession here is safe in a `fast` test:
# the gate short-circuits before the session is ever used for a network
# call, so no request leaves the process. If the gate ever regressed and
# let this sentence through to the judge, the observable failure would be
# a "judge_error" decision (from the invalid API key) or a hang/timeout --
# not a false pass -- so this test cannot silently pass for the wrong
# reason even without a mock.
@pytest.mark.fast
def test_gate_rejected_sentence_never_calls_the_judge() -> None:
    # An invalid API key would make any real judge call fail loudly
    # (GeminiClientError -> JudgeError -> a "judge_error" decision). A
    # clean "gate_violation" decision, with judge_result left None and no
    # exception raised, is real evidence the judge path was never reached
    # -- no mock or spy is needed to prove this.
    decision = asyncio.run(
        _evaluate("Will you marry me and also send me money right now?")
    )
    assert decision.approved is False
    assert decision.reason == "gate_violation"
    assert decision.judge_result is None
    assert len(decision.gate_violations) >= 2

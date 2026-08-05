"""Promptfoo custom Python provider for the T11 gates+judge qualification
and calibration run.

For each corpus row (`context["vars"]["sentence"]`), this provider calls
the real `personal_voice_msg.judging.pipeline.evaluate_message_safety`
boundary -- the same deterministic-gates-then-judge function production
code will use -- against the real Gemini API for any row that survives
the gates. Returns a stable, deterministically-assertable label:

- "APPROVED" when `SafetyDecision.approved` is True.
- "REJECTED:<reason>" (reason is `SafetyDecision.reason`, e.g.
  "gate_violation", "judge_risk_flag", "judge_score_floor", "judge_error")
  otherwise.

Never raises uncaught, per Promptfoo's Python provider protocol.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import aiohttp

from personal_voice_msg.generation.config import load_gemini_settings
from personal_voice_msg.judging.pipeline import evaluate_message_safety

# Loaded once at import time, not per trial -- a small, deliberate
# improvement over evals/t10/provider.py's per-trial reload, which the
# T11 pre-flight audit found harmless but unnecessary to repeat here.
_SETTINGS = load_gemini_settings(Path(os.environ["GEMINI_GENERATION_CONFIG"]))


async def _run_trial(sentence: str) -> str:
    async with aiohttp.ClientSession() as session:
        decision = await evaluate_message_safety(session, _SETTINGS.api_key, sentence)
    if decision.approved:
        return "APPROVED"
    return f"REJECTED:{decision.reason}"


def call_api(
    prompt: str, options: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    row_vars = context.get("vars", {})
    try:
        sentence = row_vars["sentence"]
        label = asyncio.run(_run_trial(sentence))
        return {"output": label}
    except Exception as error:  # noqa: BLE001 - Promptfoo expects a response, never a raise.
        return {
            "output": f"REJECTED:harness_error:{type(error).__name__}",
            "error": f"unexpected {type(error).__name__}",
        }

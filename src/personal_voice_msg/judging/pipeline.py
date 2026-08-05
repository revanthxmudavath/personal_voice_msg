from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from personal_voice_msg.judging.gates import GateViolation, evaluate_gates
from personal_voice_msg.judging.judge import JudgeError, JudgeResult, judge_sentence
from personal_voice_msg.redaction import SensitiveValue

# Starting values only. Task 6 calibrates these for real against the
# human-labelled corpus in evals/t11/ and records the final, evidence-
# backed values in docs/task-logs/T11.md.
SAFE_TONE_FLOOR = 6.0
SAFE_WARMTH_FLOOR = 6.0
SAFE_NATURALNESS_FLOOR = 6.0


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    approved: bool
    reason: str | None
    gate_violations: tuple[GateViolation, ...] = ()
    judge_result: JudgeResult | None = None


async def evaluate_message_safety(
    session: aiohttp.ClientSession,
    api_key: SensitiveValue[str],
    sentence: str,
) -> SafetyDecision:
    """Deterministically decide whether a generated sentence may be approved.

    Runs the local prohibition gates first; a violation rejects
    immediately without spending an API call on the judge. Only a
    gate-clean sentence reaches the structured judge. The judge's score
    and risk_flags are read by this function's own plain comparisons below
    -- the judge itself never sets `approved`, so no judge result can
    bypass this deterministic code.
    """

    gate_decision = evaluate_gates(sentence)
    if not gate_decision.accepted:
        return SafetyDecision(
            approved=False,
            reason="gate_violation",
            gate_violations=gate_decision.violations,
        )

    try:
        judge_result = await judge_sentence(session, api_key, sentence)
    except JudgeError:
        return SafetyDecision(approved=False, reason="judge_error")

    if judge_result.risk_flags:
        return SafetyDecision(
            approved=False, reason="judge_risk_flag", judge_result=judge_result
        )
    if (
        judge_result.romantic_tone_score < SAFE_TONE_FLOOR
        or judge_result.warmth_score < SAFE_WARMTH_FLOOR
        or judge_result.naturalness_score < SAFE_NATURALNESS_FLOOR
    ):
        return SafetyDecision(
            approved=False, reason="judge_score_floor", judge_result=judge_result
        )
    return SafetyDecision(approved=True, reason=None, judge_result=judge_result)

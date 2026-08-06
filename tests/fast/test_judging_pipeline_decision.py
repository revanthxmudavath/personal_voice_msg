from __future__ import annotations

import pytest

from personal_voice_msg.judging.judge import JudgeResult
from personal_voice_msg.judging.pipeline import decide_from_judge_result


@pytest.mark.fast
def test_all_scores_exactly_at_floor_approves() -> None:
    # The floor comparison is `<` (reject below floor), so `>=` the floor
    # must approve -- 6.5 exactly must not be rejected.
    result = JudgeResult(
        romantic_tone_score=6.5,
        warmth_score=6.5,
        naturalness_score=6.5,
        risk_flags=(),
        reasons="borderline but clears the inclusive floor",
    )
    decision = decide_from_judge_result(result)
    assert decision.approved is True
    assert decision.reason is None
    assert decision.judge_result is result


@pytest.mark.fast
def test_one_score_just_below_floor_rejects_via_score_floor() -> None:
    result = JudgeResult(
        romantic_tone_score=6.4,
        warmth_score=9.0,
        naturalness_score=9.0,
        risk_flags=(),
        reasons="tone just misses the floor",
    )
    decision = decide_from_judge_result(result)
    assert decision.approved is False
    assert decision.reason == "judge_score_floor"
    assert decision.judge_result is result


@pytest.mark.fast
def test_single_risk_flag_rejects_even_with_perfect_scores() -> None:
    result = JudgeResult(
        romantic_tone_score=10.0,
        warmth_score=10.0,
        naturalness_score=10.0,
        risk_flags=("possessive",),
        reasons="perfect scores but a risk flag was raised",
    )
    decision = decide_from_judge_result(result)
    assert decision.approved is False
    assert decision.reason == "judge_risk_flag"
    assert decision.judge_result is result


@pytest.mark.fast
def test_all_clear_result_approves() -> None:
    result = JudgeResult(
        romantic_tone_score=8.5,
        warmth_score=9.0,
        naturalness_score=8.8,
        risk_flags=(),
        reasons="warm, natural, no risk flags",
    )
    decision = decide_from_judge_result(result)
    assert decision.approved is True
    assert decision.reason is None
    assert decision.judge_result is result

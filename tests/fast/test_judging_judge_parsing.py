from __future__ import annotations

import pytest

from personal_voice_msg.judging.judge import (
    JudgeError,
    JudgeResult,
    _parse_judge_result,
)

VALID_STRUCTURED: dict[str, object] = {
    "romantic_tone_score": 8.5,
    "warmth_score": 9.0,
    "naturalness_score": 7.5,
    "risk_flags": [],
    "reasons": "Warm, gentle, and free of any risk signals.",
}


@pytest.mark.fast
def test_parses_a_well_formed_judge_response() -> None:
    result = _parse_judge_result(VALID_STRUCTURED)
    assert result == JudgeResult(
        romantic_tone_score=8.5,
        warmth_score=9.0,
        naturalness_score=7.5,
        risk_flags=(),
        reasons="Warm, gentle, and free of any risk signals.",
    )


@pytest.mark.fast
def test_parses_risk_flags_from_the_known_vocabulary() -> None:
    structured = dict(VALID_STRUCTURED, risk_flags=["possessive", "overly_intense"])
    result = _parse_judge_result(structured)
    assert result.risk_flags == ("possessive", "overly_intense")


@pytest.mark.fast
@pytest.mark.parametrize(
    "structured",
    [
        {k: v for k, v in VALID_STRUCTURED.items() if k != "reasons"},
        dict(VALID_STRUCTURED, romantic_tone_score="high"),
        dict(VALID_STRUCTURED, romantic_tone_score=True),
        dict(VALID_STRUCTURED, romantic_tone_score=11.0),
        dict(VALID_STRUCTURED, romantic_tone_score=-1.0),
        dict(VALID_STRUCTURED, risk_flags="possessive"),
        dict(VALID_STRUCTURED, risk_flags=["not_a_real_flag"]),
        dict(VALID_STRUCTURED, reasons=42),
        {},
    ],
    ids=[
        "missing-reasons",
        "non-numeric-score",
        "boolean-score",
        "score-above-range",
        "score-below-range",
        "risk-flags-not-a-list",
        "unknown-risk-flag",
        "reasons-not-a-string",
        "empty-payload",
    ],
)
def test_rejects_malformed_or_uncertain_judge_output(
    structured: dict[str, object],
) -> None:
    with pytest.raises(JudgeError):
        _parse_judge_result(structured)

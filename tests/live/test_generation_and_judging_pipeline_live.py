from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.database import Database
from personal_voice_msg.discovery.inspiration import (
    Emotion,
    Imagery,
    InspirationCard,
    RightsCategory,
    Theme,
    Tone,
)
from personal_voice_msg.generation.sentence import generate_sentence
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.judging.pipeline import SafetyDecision, evaluate_message_safety
from personal_voice_msg.redaction import SensitiveValue

if os.environ.get("T11_LIVE_END_TO_END") != "1":
    pytestmark = [pytest.mark.skip(reason="requires T11_LIVE_END_TO_END=1")]

_THEMES = (
    Theme.APPRECIATION,
    Theme.AFFECTION,
    Theme.COMPANIONSHIP,
    Theme.ENCOURAGEMENT,
)
CARDS = tuple(
    InspirationCard(
        theme=theme,
        emotion=Emotion.JOY,
        imagery=Imagery.OPEN_SKY,
        tone=Tone.PLAYFUL,
        source="https://example.invalid/poem",
        rights_category=RightsCategory.UNKNOWN,
        evidence="unused",
        discovery_timestamp=datetime(2026, 8, 4, tzinfo=UTC),
    )
    for theme in _THEMES
) * 4  # 16 trials against one shared, accumulating database


def _real_api_key() -> SensitiveValue[str]:
    key_file = os.environ["GEMINI_API_KEY_FILE"]
    return SensitiveValue(Path(key_file).read_text(encoding="utf-8").strip())


async def _run_all(database_path: Path) -> list[SafetyDecision | None]:
    database = Database(database_path)
    database.migrate()
    history = MessageHistory(database)
    api_key = _real_api_key()
    results: list[SafetyDecision | None] = []
    async with aiohttp.ClientSession() as session:
        for card in CARDS:
            decision = await generate_sentence(
                session, api_key, card, history, datetime.now(UTC)
            )
            if not decision.accepted:
                # Real dedup rejection against the accumulating history --
                # expected to happen at least once across 16 trials, per
                # the ~8%-collision rate recorded in docs/task-logs/T10.md.
                results.append(None)
                continue
            text = database.get_message_text(decision.recorded_message_id)
            safety = await evaluate_message_safety(session, api_key, text)
            results.append(safety)
    return results


@pytest.mark.live
def test_generation_and_safety_gate_against_a_shared_accumulating_history(
    tmp_path: Path,
) -> None:
    results = asyncio.run(_run_all(tmp_path / "accumulating-history.sqlite3"))

    generated = [safety for safety in results if safety is not None]
    assert generated, "at least one trial must produce a recorded sentence"
    for safety in generated:
        assert isinstance(safety.approved, bool)
        # The two fields must always agree: approved iff there is no
        # rejection reason. This is a real invariant of SafetyDecision,
        # not tautological -- it would fail if the pipeline's wiring
        # ever set one field without the other.
        assert safety.approved is (safety.reason is None)
        # Whenever the judge ran to completion and its scores/risk_flags
        # actually drove the decision ("judge_risk_flag"/"judge_score_floor",
        # or an approval that reached the judge), the result must have been
        # captured, not silently dropped. "judge_error" is deliberately
        # excluded here even though it shares the "judge_" prefix textually
        # -- it means the judge call itself failed, so no JudgeResult exists
        # to capture; asserting non-None there would be asserting the
        # opposite of what actually happened.
        if safety.reason in ("judge_risk_flag", "judge_score_floor") or (
            safety.approved and safety.reason is None
        ):
            assert safety.judge_result is not None

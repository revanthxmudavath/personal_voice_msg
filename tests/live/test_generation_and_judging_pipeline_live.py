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
from personal_voice_msg.judging.pipeline import evaluate_message_safety
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


async def _run_all(database_path: Path) -> list[bool | None]:
    database = Database(database_path)
    database.migrate()
    history = MessageHistory(database)
    api_key = _real_api_key()
    approvals: list[bool | None] = []
    async with aiohttp.ClientSession() as session:
        for card in CARDS:
            decision = await generate_sentence(
                session, api_key, card, history, datetime.now(UTC)
            )
            if not decision.accepted:
                # Real dedup rejection against the accumulating history --
                # expected to happen at least once across 16 trials, per
                # the ~8%-collision rate recorded in docs/task-logs/T10.md.
                approvals.append(None)
                continue
            text = database.get_message_text(decision.recorded_message_id)
            safety = await evaluate_message_safety(session, api_key, text)
            approvals.append(safety.approved)
    return approvals


@pytest.mark.live
def test_generation_and_safety_gate_against_a_shared_accumulating_history(
    tmp_path: Path,
) -> None:
    approvals = asyncio.run(_run_all(tmp_path / "accumulating-history.sqlite3"))

    generated = [approved for approved in approvals if approved is not None]
    assert generated, "at least one trial must produce a recorded sentence"
    for approved in generated:
        assert isinstance(approved, bool)

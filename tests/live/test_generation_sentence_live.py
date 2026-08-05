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
from personal_voice_msg.redaction import SensitiveValue

if os.environ.get("T10_LIVE_GENERATION") != "1":
    pytestmark = [
        pytest.mark.skip(reason="requires T10_LIVE_GENERATION=1"),
    ]

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _real_api_key() -> SensitiveValue[str]:
    key_file = os.environ["GEMINI_API_KEY_FILE"]
    return SensitiveValue(Path(key_file).read_text(encoding="utf-8").strip())


async def _generate(card: InspirationCard, history: MessageHistory) -> object:
    async with aiohttp.ClientSession() as session:
        return await generate_sentence(session, _real_api_key(), card, history, NOW)


@pytest.mark.live
def test_real_generation_produces_an_accepted_unique_sentence(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "history.sqlite3")
    database.migrate()
    history = MessageHistory(database)
    card = InspirationCard(
        theme=Theme.ENCOURAGEMENT,
        emotion=Emotion.JOY,
        imagery=Imagery.OPEN_SKY,
        tone=Tone.PLAYFUL,
        source="https://example.invalid/poem",
        rights_category=RightsCategory.UNKNOWN,
        evidence="unused",
        discovery_timestamp=NOW,
    )

    decision = asyncio.run(_generate(card, history))

    assert decision.accepted
    assert decision.recorded_message_id is not None

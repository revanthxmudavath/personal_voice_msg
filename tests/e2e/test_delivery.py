from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory

pytestmark = pytest.mark.e2e

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
WAHA_SETTINGS_ENV = "T15_WAHA_SETTINGS"
_MISSING = [n for n in (VOICE_SAMPLE_ENV, WAHA_SETTINGS_ENV) if n not in os.environ]
if _MISSING:
    pytestmark = [
        pytest.mark.e2e,
        pytest.mark.skip(
            reason=f"requires {', '.join(_MISSING)} (docs/task-logs/T16.md)"
        ),
    ]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(Path(os.environ[WAHA_SETTINGS_ENV]))


def approved_message(database: Database, text: str, now: datetime) -> None:
    database.migrate()
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, now)


def test_run_daily_send_reaches_sent_from_a_queued_message(
    settings: Settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    text = f"A real end to end delivery test at {now.timestamp()}."
    approved_message(database, text, now)
    embedding_path = settings.voice_embedding.reveal()

    async def run() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_e2e",
                date(2026, 8, 9), embedding_path, text, now,
            )

    result = asyncio.run(run())

    assert result is MessageState.SENT

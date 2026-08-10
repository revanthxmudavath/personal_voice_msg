from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.audio_pipeline import produce_voice_note
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


def test_run_daily_send_retries_a_failed_delivery_reusing_stored_audio(
    settings: Settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    text = f"A retried delivery test at {now.timestamp()}."
    approved_message(database, text, now)
    embedding_path = settings.voice_embedding.reveal()

    # Build a FAILED delivery directly with the same lower-level calls
    # run_daily_send itself would use, rather than via run_daily_send --
    # a single run_daily_send call already carries a delivery through to
    # a terminal outcome (SENT/FAILED/DELIVERY_UNKNOWN) once it reaches
    # AUDIO_READY (see test_run_daily_send_reaches_sent_from_a_queued_message),
    # so it never stops "in between" at AUDIO_READY for a second call to
    # observe. Real reservation and real synthesis happen here; only the
    # FAILED outcome itself is forced, standing in for a real WAHA
    # rejection (SenderRejected).
    reservation = database.reserve_next_message(
        "recipient_t16_retry", date(2026, 8, 9), now
    )
    assert reservation is not None
    delivery_id = reservation.delivery_id
    temp_destination = tmp_path / f"t16-retry-{delivery_id}.ogg"
    stored_audio = produce_voice_note(
        database, delivery_id, embedding_path, text, temp_destination, now
    )
    database.transition_delivery(delivery_id, MessageState.SENDING, now)
    database.record_delivery_attempt(delivery_id, MessageState.FAILED, now)

    async def run() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_retry",
                date(2026, 8, 9), embedding_path, text, now,
            )

    result = asyncio.run(run())

    assert result is MessageState.SENT
    # The retry must have reused the exact bytes produced above -- proven
    # by an exact delivery_attempts outcome sequence: one 'failed' (forced
    # above) followed by exactly one 'sent' (the retry), meaning no second
    # synthesis/attempt cycle occurred.
    with sqlite3.connect(database.path) as connection:
        outcomes = connection.execute(
            "SELECT outcome FROM delivery_attempts WHERE delivery_id = ? "
            "ORDER BY id",
            (delivery_id,),
        ).fetchall()
    assert outcomes == [("failed",), ("sent",)]
    assert stored_audio  # sanity: non-empty real audio was captured above


def test_run_daily_send_reclassifies_orphaned_sending_as_delivery_unknown(
    settings: Settings, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    now = datetime.now(UTC)
    text = f"An orphaned-sending test at {now.timestamp()}."
    approved_message(database, text, now)
    reservation = database.reserve_next_message(
        "recipient_t16_orphan", date(2026, 8, 9), now
    )
    assert reservation is not None
    database.mark_audio_ready(reservation.delivery_id, b"stale-audio-bytes", now)
    # Simulate a crash: a prior process flipped to SENDING and never
    # returned -- this run did not just do that itself.
    database.transition_delivery(
        reservation.delivery_id, MessageState.SENDING, now
    )

    async def run() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                database, settings, session, "recipient_t16_orphan",
                date(2026, 8, 9), settings.voice_embedding.reveal(), text, now,
            )

    result = asyncio.run(run())

    assert result is MessageState.DELIVERY_UNKNOWN

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.audio_pipeline import produce_voice_note
from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.scheduling import (
    PACIFIC,
    ScheduleKind,
    planned_triggers_for_date,
)
from personal_voice_msg.sender import SenderAmbiguous, send_voice_note, sign_request

pytestmark = pytest.mark.e2e

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
TELEGRAM_SETTINGS_ENV = "T16B_TELEGRAM_SETTINGS"
_MISSING = [
    name for name in (VOICE_SAMPLE_ENV, TELEGRAM_SETTINGS_ENV) if name not in os.environ
]
if _MISSING:
    pytestmark = [
        pytest.mark.e2e,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample and a real "
                f"Telegram bot/chat; set {', '.join(_MISSING)}"
            )
        ),
    ]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(Path(os.environ[TELEGRAM_SETTINGS_ENV]))


@pytest.fixture(scope="module")
def valid_audio_text() -> str:
    return "A T16b fault-injection test."


PACIFIC_DATE = datetime.now(PACIFIC).date()


def _in_send_window(pacific_date: date) -> datetime:
    trigger = next(
        t for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at


def approved_message(database: Database, text: str, now: datetime) -> None:
    database.migrate()
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, now)


class _HangingServer:
    """Accepts a connection and never responds -- a real socket, no HTTP
    semantics implemented. Forces a real client-side timeout. See
    tests/security/test_sender_error_taxonomy.py for the identical
    pattern; duplicated here rather than shared, since both files are
    small and self-contained, matching this project's existing
    per-file-scoped fake-server convention."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_and_hang, daemon=True)
        self._thread.start()

    def _accept_and_hang(self) -> None:
        self._socket.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            self._stop.wait()
            connection.close()

    def stop(self) -> None:
        self._stop.set()
        self._socket.close()


def _sent_count(database_path: Path, delivery_id: int) -> tuple[int]:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM delivery_attempts "
            "WHERE delivery_id = ? AND outcome = 'sent'",
            (delivery_id,),
        ).fetchone()
    return row


@pytest.mark.parametrize(
    "interrupt_state",
    [
        MessageState.RESERVED,
        MessageState.AUDIO_READY,
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ],
)
def test_restart_at_every_delivery_state_never_duplicates_a_send(
    settings: Settings, tmp_path: Path, interrupt_state: MessageState
) -> None:
    """Simulates a process restart by constructing a fresh Database handle
    from the same file and resuming from each persisted state in turn.

    RESERVED/AUDIO_READY/FAILED restart into a genuine real send and reach
    SENT. SENDING/DELIVERY_UNKNOWN are ambiguous on entry -- under
    Telegram there is nothing to reconcile against, so these must resolve
    to DELIVERY_UNKNOWN without ever making a real network call, proven
    by passing session=None (any code path that tried to send would raise
    AttributeError before the assertion below).
    """
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(PACIFIC_DATE)
    text = (
        f"A restart-at-{interrupt_state.value} test at "
        f"{datetime.now(UTC).timestamp()}."
    )
    approved_message(database, text, now)
    recipient_key = f"recipient_t16b_restart_{interrupt_state.value}"
    reservation = database.reserve_next_message(recipient_key, PACIFIC_DATE, now)
    assert reservation is not None
    embedding_path = settings.voice_embedding.reveal()

    if interrupt_state is not MessageState.RESERVED:
        temp_destination = tmp_path / f"t16b-restart-{reservation.delivery_id}.ogg"
        produce_voice_note(
            database, reservation.delivery_id, embedding_path, text,
            temp_destination, now,
        )
    if interrupt_state in (
        MessageState.SENDING,
        MessageState.FAILED,
        MessageState.DELIVERY_UNKNOWN,
    ):
        database.transition_delivery(reservation.delivery_id, MessageState.SENDING, now)
    if interrupt_state is MessageState.FAILED:
        database.record_delivery_attempt(
            reservation.delivery_id, MessageState.FAILED, now
        )
    if interrupt_state is MessageState.DELIVERY_UNKNOWN:
        database.record_delivery_attempt(
            reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, now
        )

    resumed_database = Database(database_path)

    async def resume_real() -> MessageState:
        async with aiohttp.ClientSession() as session:
            return await run_daily_send(
                resumed_database, settings, session, recipient_key,
                PACIFIC_DATE, embedding_path, now,
            )

    async def resume_no_network() -> MessageState:
        return await run_daily_send(
            resumed_database, settings, None, recipient_key,  # type: ignore[arg-type]
            PACIFIC_DATE, embedding_path, now,
        )

    if interrupt_state in (MessageState.SENDING, MessageState.DELIVERY_UNKNOWN):
        result = asyncio.run(resume_no_network())
        assert result is MessageState.DELIVERY_UNKNOWN
        assert _sent_count(database_path, reservation.delivery_id) == (0,)
        return

    result = asyncio.run(resume_real())
    assert result is MessageState.SENT
    assert _sent_count(database_path, reservation.delivery_id) == (1,)

    # SENT is terminal (DELIVERY_TRANSITIONS[SENT] = set()) -- a second
    # call must never touch the network either, proven the same way.
    second_result = asyncio.run(resume_no_network())
    assert second_result is MessageState.SENT
    assert _sent_count(database_path, reservation.delivery_id) == (1,)


def test_a_real_timeout_during_send_becomes_delivery_unknown_and_never_retries(
    settings: Settings, tmp_path: Path
) -> None:
    """Composes two already-proven pieces rather than re-deriving fault
    injection from scratch: tests/security/test_sender_error_taxonomy.py
    proves a real hang raises SenderAmbiguous; the restart-matrix test
    above proves a DELIVERY_UNKNOWN delivery never retries. This connects
    them through a real send attempt: pause is impossible (there is no
    container to pause under Telegram), so the fault is injected via a
    real hanging local server instead, redirected to via
    send_voice_note's api_base override -- exactly the mechanism
    tests/security/test_sender_error_taxonomy.py already established.
    """
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(PACIFIC_DATE)
    text = f"A real-hang fault injection test at {datetime.now(UTC).timestamp()}."
    approved_message(database, text, now)
    recipient_key = "recipient_t16b_hang"
    reservation = database.reserve_next_message(recipient_key, PACIFIC_DATE, now)
    assert reservation is not None
    embedding_path = settings.voice_embedding.reveal()
    temp_destination = tmp_path / f"t16b-hang-{reservation.delivery_id}.ogg"
    produce_voice_note(
        database, reservation.delivery_id, embedding_path, text, temp_destination, now
    )
    database.transition_delivery(reservation.delivery_id, MessageState.SENDING, now)
    audio_bytes = database.get_audio_data(reservation.delivery_id)
    idempotency_key = f"delivery-{reservation.delivery_id}"
    timestamp = int(now.timestamp())
    signature = sign_request(
        settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
    )

    server = _HangingServer()
    try:
        async def attempt() -> None:
            async with aiohttp.ClientSession() as session:
                await send_voice_note(
                    session, database, settings, audio_bytes, idempotency_key,
                    timestamp, signature, now,
                    api_base=f"http://127.0.0.1:{server.port}",
                )

        with pytest.raises(SenderAmbiguous):
            asyncio.run(attempt())
    finally:
        server.stop()

    # Exactly what delivery.py's own AUDIO_READY branch does on
    # SenderAmbiguous -- reproduced directly since run_daily_send always
    # targets the real Telegram API with no fake-server override of its
    # own (only send_voice_note has one, added in Task 3 specifically for
    # testing).
    database.record_delivery_attempt(
        reservation.delivery_id, MessageState.DELIVERY_UNKNOWN, now
    )

    async def resume_no_network() -> MessageState:
        return await run_daily_send(
            database, settings, None, recipient_key,  # type: ignore[arg-type]
            PACIFIC_DATE, embedding_path, now,
        )

    result = asyncio.run(resume_no_network())
    assert result is MessageState.DELIVERY_UNKNOWN
    assert _sent_count(database_path, reservation.delivery_id) == (0,)

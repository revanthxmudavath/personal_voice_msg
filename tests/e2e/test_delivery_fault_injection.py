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
from personal_voice_msg.database import (
    Database,
    MessageState,
    recipient_key_for_chat_id,
)
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.scheduling import (
    PACIFIC,
    ScheduleKind,
    planned_triggers_for_date,
)

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


class _FixedStatusServer:
    """Accepts one connection, drains whatever the client sends until it
    goes quiet, then responds with a fixed HTTP status line and body, then
    closes -- a real raw socket, no aiohttp/Telegram server semantics
    beyond the status line and body text. See
    tests/security/test_sender_error_taxonomy.py for the identical
    pattern; duplicated here rather than shared, matching this file's own
    existing per-file-scoped fake-server convention (see _HangingServer
    above)."""

    def __init__(self, status_line: str, body: bytes) -> None:
        self._status_line = status_line
        self._body = body
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_and_respond, daemon=True)
        self._thread.start()

    def _accept_and_respond(self) -> None:
        self._socket.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            try:
                connection.settimeout(2.0)
                try:
                    while connection.recv(65_536):
                        pass
                except (TimeoutError, OSError):
                    pass
                response = (
                    f"{self._status_line}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(self._body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode() + self._body
                connection.sendall(response)
            finally:
                connection.close()
            return

    def stop(self) -> None:
        self._stop.set()
        self._socket.close()


# Not the independent proof of "no duplicate send" on its own -- under
# this design DELIVERY_TRANSITIONS[SENT] = set() makes a second 'sent'
# row structurally impossible once one exists (database.py), so this
# count is a consistency check, not the primary evidence. The primary
# evidence is the session=None calls above/below: any code path that
# attempted a real send would raise before reaching these assertions.
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
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
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
    """Real fault injection through the actual production orchestrator,
    not a hand-reproduced imitation of it: run_daily_send's own
    AUDIO_READY branch is exercised directly against a real hanging local
    server via the api_base override, so this test proves what
    delivery.py itself does on a real ambiguous outcome, not what this
    test file assumes it does.
    """
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(PACIFIC_DATE)
    text = f"A real-hang fault injection test at {datetime.now(UTC).timestamp()}."
    approved_message(database, text, now)
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
    reservation = database.reserve_next_message(recipient_key, PACIFIC_DATE, now)
    assert reservation is not None
    embedding_path = settings.voice_embedding.reveal()
    temp_destination = tmp_path / f"t16b-hang-{reservation.delivery_id}.ogg"
    produce_voice_note(
        database, reservation.delivery_id, embedding_path, text, temp_destination, now
    )

    server = _HangingServer()
    try:
        async def attempt() -> MessageState:
            async with aiohttp.ClientSession() as session:
                return await run_daily_send(
                    database, settings, session, recipient_key,
                    PACIFIC_DATE, embedding_path, now,
                    api_base=f"http://127.0.0.1:{server.port}",
                )

        result = asyncio.run(attempt())
    finally:
        server.stop()

    assert result is MessageState.DELIVERY_UNKNOWN
    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.DELIVERY_UNKNOWN
    )
    assert _sent_count(database_path, reservation.delivery_id) == (0,)

    async def resume_no_network() -> MessageState:
        return await run_daily_send(
            database, settings, None, recipient_key,  # type: ignore[arg-type]
            PACIFIC_DATE, embedding_path, now,
        )

    second_result = asyncio.run(resume_no_network())
    assert second_result is MessageState.DELIVERY_UNKNOWN
    assert _sent_count(database_path, reservation.delivery_id) == (0,)


def test_a_real_blocked_by_user_403_disables_sending_and_fails_the_delivery(
    settings: Settings, tmp_path: Path
) -> None:
    """T17 review finding F3: nothing in the suite previously drove
    run_daily_send all the way to AUDIO_READY, got a real 403
    'blocked by the user' response, and asserted that
    disable_sending(BLOCKED_BY_USER) actually fires and the delivery ends
    up FAILED. Real fault injection through the actual production
    orchestrator via the api_base override, mirroring
    test_a_real_timeout_during_send_becomes_delivery_unknown_and_never_retries
    above -- a real local server, not a hand-reproduced imitation of
    delivery.py's except SenderBlocked branch.
    """
    database_path = tmp_path / "state.sqlite3"
    database = Database(database_path)
    now = _in_send_window(PACIFIC_DATE)
    text = (
        f"A real-blocked-403 fault injection test at {datetime.now(UTC).timestamp()}."
    )
    approved_message(database, text, now)
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
    reservation = database.reserve_next_message(recipient_key, PACIFIC_DATE, now)
    assert reservation is not None
    embedding_path = settings.voice_embedding.reveal()
    temp_destination = tmp_path / f"t17-blocked-{reservation.delivery_id}.ogg"
    produce_voice_note(
        database, reservation.delivery_id, embedding_path, text, temp_destination, now
    )

    assert database.is_sending_enabled() is True

    server = _FixedStatusServer(
        "HTTP/1.1 403 Forbidden",
        body=(
            b'{"ok":false,"error_code":403,'
            b'"description":"Forbidden: bot was blocked by the user"}'
        ),
    )
    try:
        async def attempt() -> MessageState:
            async with aiohttp.ClientSession() as session:
                return await run_daily_send(
                    database, settings, session, recipient_key,
                    PACIFIC_DATE, embedding_path, now,
                    api_base=f"http://127.0.0.1:{server.port}",
                )

        result = asyncio.run(attempt())
    finally:
        server.stop()

    assert result is MessageState.FAILED
    assert (
        database.get_delivery_state(reservation.delivery_id)
        is MessageState.FAILED
    )
    assert database.is_sending_enabled() is False
    assert _sent_count(database_path, reservation.delivery_id) == (0,)

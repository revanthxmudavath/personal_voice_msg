from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import threading
from datetime import date, datetime
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.config import RuntimeProfile, Settings
from personal_voice_msg.daily_send_entrypoint import run_daily_entrypoint
from personal_voice_msg.database import (
    Database,
    MessageState,
    recipient_key_for_chat_id,
)
from personal_voice_msg.history import MessageHistory
from personal_voice_msg.redaction import SensitiveValue
from personal_voice_msg.scheduling import ScheduleKind, planned_triggers_for_date
from personal_voice_msg.voice_enrollment import enroll_voice

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
ENROLLED_CHAT_ID = 424242

pytestmark = pytest.mark.security

# Only the two tests that actually drive produce_voice_note's real
# TTS/FFmpeg pipeline need a real consented voice sample. The STOP test
# below never reaches synthesis (sending is disabled before the RESERVED
# branch that calls produce_voice_note -- see delivery.py's
# `if not sending_enabled: return state` early return) and must run
# everywhere so this branch's core security property -- a STOP received
# during a call's own poll blocks that same call's send -- is not skipped
# in every environment without a real sample, including this sandbox and
# CI.
requires_voice_sample = pytest.mark.skipif(
    VOICE_SAMPLE_ENV not in os.environ,
    reason=(
        "requires a real consented test voice sample so "
        "produce_voice_note's real TTS/FFmpeg pipeline has "
        f"something real to synthesize; set {VOICE_SAMPLE_ENV}"
    ),
)


class _RoutingServer:
    """Accepts connections one at a time, in a loop, until stopped.
    Drains each request until the connection goes quiet (matching
    _FixedStatusServer's established pattern in
    tests/security/test_sender_error_taxonomy.py -- a real raw socket, no
    aiohttp/Telegram server semantics beyond the status line, headers,
    and body), extracts the request path from the first line, and
    responds with whichever of ``routes`` matches a substring of that
    path. Records every route seen, in order (with any leading
    ``/bot<token>`` segment stripped -- see ``_route_only``, so a real
    bot token used during Task 5's live verification never ends up in
    ``paths_seen``, and thus never in a pytest failure message), so a
    test can assert not just what each endpoint returned but the order
    -- or absence -- of the calls that reached it.
    """

    def __init__(self, routes: dict[str, tuple[str, bytes]]) -> None:
        self._routes = routes
        self.paths_seen: list[str] = []
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._socket.settimeout(1.0)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except (TimeoutError, OSError):
                # OSError covers accept() unblocking because stop()
                # closed the listening socket out from under it -- not a
                # real fault, just this loop's own shutdown signal.
                continue
            try:
                connection.settimeout(2.0)
                buffer = b""
                try:
                    while True:
                        chunk = connection.recv(65_536)
                        if not chunk:
                            break
                        buffer += chunk
                except (TimeoutError, OSError):
                    pass
                request_line = buffer.split(b"\r\n", 1)[0].decode(errors="replace")
                path = request_line.split(" ")[1] if " " in request_line else ""
                self.paths_seen.append(self._route_only(path))
                status_line, body = self._match(path)
                response = (
                    f"{status_line}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode() + body
                connection.sendall(response)
            finally:
                connection.close()

    def _match(self, path: str) -> tuple[str, bytes]:
        for substring, response in self._routes.items():
            if substring in path:
                return response
        return ("HTTP/1.1 404 Not Found", b'{"ok":false}')

    @staticmethod
    def _route_only(path: str) -> str:
        """Strip a leading ``/bot<token>`` segment, if present, so the
        bot token never appears in ``paths_seen``. Routing itself
        (``_match``) still uses the full, unredacted ``path``."""
        marker = "/bot"
        if not path.startswith(marker):
            return path
        remainder = path[len(marker):]
        slash = remainder.find("/")
        return remainder[slash:] if slash != -1 else path

    def stop(self) -> None:
        self._stop.set()
        self._socket.close()


@pytest.fixture(scope="module")
def embedding_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    workdir = tmp_path_factory.mktemp("t17b_entrypoint_audio")
    raw_sample = workdir / "raw_sample.wav"
    shutil.copyfile(Path(os.environ[VOICE_SAMPLE_ENV]), raw_sample)
    embedding = workdir / "voice_embedding.safetensors"
    enroll_voice(raw_sample, embedding)
    return embedding


def _settings(embedding_path: Path) -> Settings:
    return Settings(
        profile=RuntimeProfile.DEVELOPMENT,
        telegram_chat_id=SensitiveValue(ENROLLED_CHAT_ID),
        telegram_bot_token=SensitiveValue("test-token"),
        voice_embedding=SensitiveValue(embedding_path),
        sender_auth_key=SensitiveValue("test-sender-auth-key"),
    )


def _approved_message_in_window(database: Database, text: str) -> tuple[date, datetime]:
    pacific_date = date(2026, 8, 9)
    trigger = next(
        t
        for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    now = trigger.scheduled_at
    decision = MessageHistory(database).evaluate_and_record(text, now)
    assert decision.accepted
    assert decision.recorded_message_id is not None
    database.approve_message(decision.recorded_message_id, now)
    return pacific_date, now


@requires_voice_sample
def test_a_non_stop_poll_is_followed_by_a_real_send_in_order(
    embedding_path: Path, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    settings = _settings(embedding_path)
    pacific_date, now = _approved_message_in_window(
        database, "T17b entrypoint order test."
    )
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())

    server = _RoutingServer(
        {
            "getUpdates": ("HTTP/1.1 200 OK", b'{"ok":true,"result":[]}'),
            "sendVoice": (
                "HTTP/1.1 200 OK",
                b'{"ok":true,"result":{"message_id":1}}',
            ),
        }
    )
    try:
        async def attempt() -> MessageState | None:
            async with aiohttp.ClientSession() as session:
                return await run_daily_entrypoint(
                    database, settings, session, recipient_key, pacific_date,
                    embedding_path, now,
                    api_base=f"http://127.0.0.1:{server.port}",
                )

        result = asyncio.run(attempt())
    finally:
        server.stop()

    assert result is MessageState.SENT
    assert len(server.paths_seen) == 2
    assert "getUpdates" in server.paths_seen[0]
    assert "sendVoice" in server.paths_seen[1]


def test_a_stop_received_in_the_same_call_prevents_the_send_that_would_follow(
    tmp_path: Path,
) -> None:
    """The offset-cursor poll happens before run_daily_send re-reads
    is_sending_enabled -- a STOP arriving in this call's own poll must
    already have taken effect by the time the send would otherwise be
    attempted, proven here by the sendVoice route never being hit.

    Deliberately does not depend on the module's ``embedding_path``
    fixture (which requires a real T13_VOICE_SAMPLE): once sending is
    disabled, run_daily_send's `if not sending_enabled: return state`
    early return (delivery.py) fires before the RESERVED branch that
    would call produce_voice_note, so this path never opens the
    embedding file. A path that doesn't exist is enough."""
    embedding_path = tmp_path / "unused_embedding.safetensors"
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    settings = _settings(embedding_path)
    pacific_date, now = _approved_message_in_window(
        database, "T17b stop-in-call test."
    )
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
    stop_body = json.dumps(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": ENROLLED_CHAT_ID},
                        "text": "STOP",
                    },
                }
            ],
        }
    ).encode()

    server = _RoutingServer(
        {
            "getUpdates": ("HTTP/1.1 200 OK", stop_body),
            "sendVoice": (
                "HTTP/1.1 200 OK",
                b'{"ok":true,"result":{"message_id":1}}',
            ),
        }
    )
    try:
        async def attempt() -> MessageState | None:
            async with aiohttp.ClientSession() as session:
                return await run_daily_entrypoint(
                    database, settings, session, recipient_key, pacific_date,
                    embedding_path, now,
                    api_base=f"http://127.0.0.1:{server.port}",
                )

        result = asyncio.run(attempt())
    finally:
        server.stop()

    assert database.is_sending_enabled() is False
    assert result is MessageState.QUEUED
    assert len(server.paths_seen) == 1
    assert "getUpdates" in server.paths_seen[0]


@requires_voice_sample
def test_a_malformed_getupdates_response_does_not_prevent_the_send_that_follows(
    embedding_path: Path, tmp_path: Path
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    settings = _settings(embedding_path)
    pacific_date, now = _approved_message_in_window(
        database, "T17b poll-error test."
    )
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())

    server = _RoutingServer(
        {
            "getUpdates": ("HTTP/1.1 200 OK", b"not valid json"),
            "sendVoice": (
                "HTTP/1.1 200 OK",
                b'{"ok":true,"result":{"message_id":1}}',
            ),
        }
    )
    try:
        async def attempt() -> MessageState | None:
            async with aiohttp.ClientSession() as session:
                return await run_daily_entrypoint(
                    database, settings, session, recipient_key, pacific_date,
                    embedding_path, now,
                    api_base=f"http://127.0.0.1:{server.port}",
                )

        result = asyncio.run(attempt())
    finally:
        server.stop()

    assert result is MessageState.SENT
    assert database.is_sending_enabled() is True
    assert len(server.paths_seen) == 2

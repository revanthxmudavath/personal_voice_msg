from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiohttp

from personal_voice_msg.config import Settings
from personal_voice_msg.database import Database, DisableReason

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 65_536
RESPONSE_CHUNK_BYTES = 8_192
STOP_TEXT = "STOP"


class TelegramPollError(RuntimeError):
    """Raised on a network failure or a malformed/non-ok getUpdates
    response. The inbound offset cursor is left untouched, so the next
    poll retries the same batch instead of silently losing it -- see
    docs/superpowers/specs/2026-08-19-t17-telegram-consent-stop-killswitch-design.md.
    """


def _process_updates(
    payload: Any, enrolled_chat_id: int
) -> tuple[int | None, bool]:
    """Pure and synchronous on purpose -- no I/O -- so the matching logic
    can be unit-tested directly with real Telegram-shaped payloads,
    matching this project's real-data-over-mocks testing policy (compare
    recipient_enrollment.py's ``_extract_chat_id``).

    Returns ``(new_offset, stop_found)``. ``new_offset`` is ``None`` when
    the batch was empty (nothing to acknowledge); otherwise it is one past
    the highest ``update_id`` seen, Telegram's own documented
    acknowledgment convention. A message from any chat other than
    ``enrolled_chat_id`` is inspected only far enough to be ignored --
    never compared against STOP_TEXT, never causing any other effect.
    """

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramPollError(f"Telegram getUpdates failed: {payload!r}")
    updates = payload.get("result")
    if not isinstance(updates, list):
        raise TelegramPollError("Telegram getUpdates response was malformed")

    max_update_id: int | None = None
    stop_found = False
    for update in updates:
        if not isinstance(update, dict):
            raise TelegramPollError("Telegram getUpdates response was malformed")
        update_id = update.get("update_id")
        if type(update_id) is not int:
            raise TelegramPollError("Telegram getUpdates response was malformed")
        if max_update_id is None or update_id > max_update_id:
            max_update_id = update_id

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if chat_id == enrolled_chat_id and message.get("text") == STOP_TEXT:
            stop_found = True

    new_offset = None if max_update_id is None else max_update_id + 1
    return new_offset, stop_found


async def poll_inbound_stop(
    session: aiohttp.ClientSession,
    database: Database,
    settings: Settings,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,
) -> bool:
    """Poll Telegram's getUpdates once, using the durably-stored offset
    cursor, and durably disable sending on an exact STOP from the
    enrolled telegram_chat_id. Returns whether sending is disabled after
    this call (either just now, or already disabled beforehand).

    Not wired to any scheduler -- no scheduler task exists yet. Callers
    (a future daily-window orchestrator) call this once per window, per
    IMPLEMENTATION_PLAN.md's T17 section.
    """

    offset = database.get_telegram_inbound_offset()
    params: dict[str, str] = {"timeout": "0"}
    if offset is not None:
        params["offset"] = str(offset)
    bot_token = settings.telegram_bot_token.reveal()

    try:
        async with session.get(
            f"{api_base}/bot{bot_token}/getUpdates",
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            allow_redirects=False,
        ) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.content.iter_chunked(RESPONSE_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
    except (aiohttp.ClientError, TimeoutError):
        raise TelegramPollError("no response received from Telegram") from None

    if total > MAX_RESPONSE_BYTES:
        raise TelegramPollError("Telegram response exceeded the size limit")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise TelegramPollError("Telegram response was not valid JSON") from None

    new_offset, stop_found = _process_updates(
        payload, settings.telegram_chat_id.reveal()
    )

    if stop_found:
        database.disable_sending(DisableReason.STOP_COMMAND, now)
    if new_offset is not None:
        database.set_telegram_inbound_offset(new_offset, now)

    return not database.is_sending_enabled()

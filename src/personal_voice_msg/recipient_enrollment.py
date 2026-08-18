from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp

from personal_voice_msg.config import RuntimeProfile

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_TELEGRAM_CHAT_ID = 2**52


class EnrollmentError(ValueError):
    """Raised when recipient enrollment cannot be completed safely."""


def _extract_chat_id(payload: dict[str, Any]) -> int:
    """Pure and synchronous on purpose -- no network I/O -- so the parsing
    logic can be unit-tested directly with real Python data structures,
    matching this project's real-data-over-mocks testing policy.
    """

    if not payload.get("ok"):
        raise EnrollmentError(f"Telegram getUpdates failed: {payload}")

    updates = payload.get("result")
    if not isinstance(updates, list):
        raise EnrollmentError("Telegram getUpdates response was malformed")

    messages = [
        update["message"]
        for update in updates
        if isinstance(update, dict) and isinstance(update.get("message"), dict)
    ]
    if not messages:
        raise EnrollmentError(
            "no inbound message found -- ask the recipient to message the "
            "bot first, then retry enrollment"
        )

    chat = messages[-1].get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    # Explicitly excludes bool -- see config.py's _telegram_chat_id for the
    # same guard and why it matters (JSON true/false deserialize to
    # Python bool, and isinstance(True, int) is True).
    if (
        type(chat_id) is not int
        or chat_id <= 0
        or chat_id >= MAX_TELEGRAM_CHAT_ID
    ):
        raise EnrollmentError("Telegram chat id was not a valid integer")

    return chat_id


async def enroll_recipient(
    bot_token: str, destination: Path, profile: RuntimeProfile
) -> int:
    """One-time: poll Telegram's getUpdates once, capture the chat_id of
    whoever sent the most recent inbound message, and write it to
    ``destination`` as the fixed allowlisted recipient.

    Refuses to run at all if ``destination`` already exists -- the
    captured chat_id becomes immutable once enrolled, matching
    voice_enrollment's trust model (there, the enrolled artifact's
    existence isn't separately guarded because ``enroll_voice`` deletes
    its own input sample after success; here there is no equivalent
    single-use input to delete, so the guard is on the output instead).

    Before running this, the owner must have already sent the recipient
    the bot's private t.me/<name> link and had them send it any message
    (conventionally /start) -- see the design spec's "Recipient
    enrollment" section.
    """

    if destination.exists():
        raise EnrollmentError(f"a recipient is already enrolled at {destination}")

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/getUpdates",
            params={"timeout": "0"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as response:
            payload = await response.json()

    chat_id = _extract_chat_id(payload)

    destination.write_text(
        json.dumps({"profile": profile.value, "telegram_chat_id": chat_id}),
        encoding="utf-8",
    )
    return chat_id

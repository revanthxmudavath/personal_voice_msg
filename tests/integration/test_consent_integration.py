from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.config import load_settings
from personal_voice_msg.consent import poll_inbound_stop
from personal_voice_msg.database import Database

pytestmark = pytest.mark.integration

TELEGRAM_SETTINGS_ENV = "T16B_TELEGRAM_SETTINGS"
_MISSING = [n for n in (TELEGRAM_SETTINGS_ENV,) if n not in os.environ]
if _MISSING:
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(
            reason=(
                "requires a real Telegram bot token/chat id, and a real "
                "'STOP' message already sent by hand from the enrolled "
                f"chat to that bot before running; set {', '.join(_MISSING)}"
            )
        ),
    ]


def test_a_real_exact_stop_from_the_enrolled_chat_disables_sending_durably(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    settings = load_settings(Path(os.environ[TELEGRAM_SETTINGS_ENV]))
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    now = datetime.now(UTC)

    async def poll() -> bool:
        async with aiohttp.ClientSession() as session:
            return await poll_inbound_stop(session, database, settings, now)

    disabled = asyncio.run(poll())

    assert disabled is True
    assert database.is_sending_enabled() is False

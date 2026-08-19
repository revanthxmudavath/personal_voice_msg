from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from personal_voice_msg.config import RuntimeProfile
from personal_voice_msg.recipient_enrollment import enroll_recipient

pytestmark = pytest.mark.integration

BOT_TOKEN_ENV = "T16B_TEST_BOT_TOKEN"
_MISSING = [name for name in (BOT_TOKEN_ENV,) if name not in os.environ]
if _MISSING:
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(
            reason=(
                "requires a real test Telegram bot token with at least one "
                f"real inbound message already sent to it; set {BOT_TOKEN_ENV}"
            )
        ),
    ]


def test_enroll_recipient_captures_a_real_chat_id(tmp_path: Path) -> None:
    destination = tmp_path / "telegram_chat_id.json"

    chat_id = asyncio.run(
        enroll_recipient(
            os.environ[BOT_TOKEN_ENV], destination, RuntimeProfile.DEVELOPMENT
        )
    )

    assert isinstance(chat_id, int) and chat_id > 0
    assert destination.exists()


def test_enroll_recipient_refuses_a_second_enrollment(tmp_path: Path) -> None:
    destination = tmp_path / "telegram_chat_id.json"
    asyncio.run(
        enroll_recipient(
            os.environ[BOT_TOKEN_ENV], destination, RuntimeProfile.DEVELOPMENT
        )
    )

    from personal_voice_msg.recipient_enrollment import EnrollmentError

    with pytest.raises(EnrollmentError, match="already enrolled"):
        asyncio.run(
            enroll_recipient(
                os.environ[BOT_TOKEN_ENV], destination, RuntimeProfile.DEVELOPMENT
            )
        )

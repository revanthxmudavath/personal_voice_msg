from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from personal_voice_msg.config import RuntimeProfile
from personal_voice_msg.recipient_enrollment import (
    EnrollmentError,
    _extract_chat_id,
    enroll_recipient,
)


@pytest.mark.fast
def test_extract_chat_id_reads_the_chat_id_when_all_messages_agree() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 222}}},
            {"update_id": 2, "message": {"chat": {"id": 222}}},
        ],
    }

    assert _extract_chat_id(payload) == 222


@pytest.mark.fast
def test_extract_chat_id_rejects_more_than_one_distinct_chat() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 111}}},
            {"update_id": 2, "message": {"chat": {"id": 222}}},
        ],
    }

    with pytest.raises(EnrollmentError, match="more than one chat"):
        _extract_chat_id(payload)


@pytest.mark.fast
def test_extract_chat_id_ignores_non_message_updates() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 1, "edited_message": {"chat": {"id": 999}}},
            {"update_id": 2, "message": {"chat": {"id": 333}}},
        ],
    }

    assert _extract_chat_id(payload) == 333


@pytest.mark.fast
def test_extract_chat_id_raises_when_no_message_found() -> None:
    payload = {"ok": True, "result": []}

    with pytest.raises(EnrollmentError, match="no inbound message"):
        _extract_chat_id(payload)


@pytest.mark.fast
def test_extract_chat_id_raises_when_telegram_reports_failure() -> None:
    payload = {"ok": False, "error_code": 401, "description": "Unauthorized"}

    with pytest.raises(EnrollmentError, match="getUpdates failed"):
        _extract_chat_id(payload)


@pytest.mark.fast
@pytest.mark.parametrize("bad_chat_id", [True, False, "123", 1.5, None, -5, 0])
def test_extract_chat_id_rejects_non_positive_integer_chat_id(
    bad_chat_id: object,
) -> None:
    payload = {
        "ok": True,
        "result": [{"update_id": 1, "message": {"chat": {"id": bad_chat_id}}}],
    }

    with pytest.raises(EnrollmentError, match="valid integer"):
        _extract_chat_id(payload)


@pytest.mark.fast
def test_refuses_to_overwrite_an_already_enrolled_recipient(tmp_path: Path) -> None:
    destination = tmp_path / "telegram_chat_id.json"
    destination.write_text(
        json.dumps({"profile": "development", "telegram_chat_id": 1}),
        encoding="utf-8",
    )

    with pytest.raises(EnrollmentError, match="already enrolled"):
        asyncio.run(
            enroll_recipient("unused-token", destination, RuntimeProfile.DEVELOPMENT)
        )

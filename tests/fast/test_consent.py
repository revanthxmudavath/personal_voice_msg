from __future__ import annotations

import pytest

from personal_voice_msg.consent import TelegramPollError, _process_updates


@pytest.mark.fast
def test_process_updates_finds_an_exact_stop_from_the_enrolled_chat() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 100,
                "message": {"chat": {"id": 555}, "text": "STOP"},
            }
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 101
    assert stop_found is True


@pytest.mark.fast
def test_process_updates_ignores_stop_from_a_different_chat() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 200,
                "message": {"chat": {"id": 999}, "text": "STOP"},
            }
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 201
    assert stop_found is False


@pytest.mark.fast
def test_process_updates_ignores_non_stop_text_from_the_enrolled_chat() -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 300,
                "message": {"chat": {"id": 555}, "text": "hello"},
            }
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 301
    assert stop_found is False


@pytest.mark.fast
@pytest.mark.parametrize("text", ["stop", "Stop", " STOP", "STOP ", "STOP!"])
def test_process_updates_requires_an_exact_case_sensitive_untrimmed_match(
    text: str,
) -> None:
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 400,
                "message": {"chat": {"id": 555}, "text": text},
            }
        ],
    }

    _, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert stop_found is False


@pytest.mark.fast
def test_process_updates_ignores_non_message_updates() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 500, "edited_message": {"chat": {"id": 555}, "text": "STOP"}},
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 501
    assert stop_found is False


@pytest.mark.fast
def test_process_updates_returns_none_offset_for_an_empty_batch() -> None:
    payload = {"ok": True, "result": []}

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset is None
    assert stop_found is False


@pytest.mark.fast
def test_process_updates_advances_offset_past_the_highest_update_id_seen() -> None:
    payload = {
        "ok": True,
        "result": [
            {"update_id": 10, "message": {"chat": {"id": 999}, "text": "hi"}},
            {"update_id": 12, "message": {"chat": {"id": 555}, "text": "STOP"}},
            {"update_id": 11, "message": {"chat": {"id": 999}, "text": "hi"}},
        ],
    }

    new_offset, stop_found = _process_updates(payload, enrolled_chat_id=555)

    assert new_offset == 13
    assert stop_found is True


@pytest.mark.fast
def test_process_updates_raises_when_telegram_reports_failure() -> None:
    payload = {"ok": False, "error_code": 401, "description": "Unauthorized"}

    with pytest.raises(TelegramPollError, match="getUpdates failed"):
        _process_updates(payload, enrolled_chat_id=555)


@pytest.mark.fast
def test_process_updates_raises_when_result_is_not_a_list() -> None:
    payload = {"ok": True, "result": "not-a-list"}

    with pytest.raises(TelegramPollError, match="malformed"):
        _process_updates(payload, enrolled_chat_id=555)


@pytest.mark.fast
def test_process_updates_raises_when_an_update_id_is_missing_or_invalid() -> None:
    payload = {
        "ok": True,
        "result": [{"message": {"chat": {"id": 555}, "text": "STOP"}}],
    }

    with pytest.raises(TelegramPollError, match="malformed"):
        _process_updates(payload, enrolled_chat_id=555)

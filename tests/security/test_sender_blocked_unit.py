"""Unit tests for _is_blocked_by_user helper function.

These tests do not require T13_VOICE_SAMPLE or real voice/network fixtures
since _is_blocked_by_user is a pure, synchronous function.
"""

from __future__ import annotations

import pytest

from personal_voice_msg.sender import _is_blocked_by_user


@pytest.mark.security
def test_is_blocked_by_user_handles_malformed_utf8_gracefully() -> None:
    """_is_blocked_by_user must handle malformed UTF-8/UTF-16 bytes that
    would trigger UnicodeDecodeError in json.loads, returning False rather
    than raising -- called unconditionally on every 403, so must never
    crash send_voice_note."""

    # Invalid UTF-8 start byte
    assert _is_blocked_by_user(b"\x80\x81\x82") is False

    # UTF-16 BOM-like prefix followed by invalid data
    assert _is_blocked_by_user(b"\xff\xfe\x00\x01invalid") is False

    # Valid blocked-by-user response still works
    assert (
        _is_blocked_by_user(
            b'{"ok":false,"error_code":403,'
            b'"description":"Forbidden: bot was blocked by the user"}'
        )
        is True
    )

    # Different 403 reason returns False
    assert (
        _is_blocked_by_user(
            b'{"ok":false,"error_code":403,'
            b'"description":"Forbidden: unauthorized"}'
        )
        is False
    )

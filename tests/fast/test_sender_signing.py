from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from personal_voice_msg.sender import is_fresh, sign_request, verify_signature

KEY = b"test-sender-auth-key"
IDEMPOTENCY_KEY = "delivery-recipient_staging_test-2026-08-08"
TIMESTAMP = 1_700_000_000
NOW = datetime.fromtimestamp(TIMESTAMP, tz=UTC)


@pytest.mark.fast
def test_sign_request_matches_a_manually_computed_hmac() -> None:
    expected = hmac.new(
        KEY,
        f"{TIMESTAMP}:{IDEMPOTENCY_KEY}".encode(),
        hashlib.sha256,
    ).hexdigest()

    assert sign_request(KEY, IDEMPOTENCY_KEY, TIMESTAMP) == expected


@pytest.mark.fast
def test_sign_request_differs_for_a_different_idempotency_key() -> None:
    signature = sign_request(KEY, IDEMPOTENCY_KEY, TIMESTAMP)

    assert sign_request(KEY, "a-different-key", TIMESTAMP) != signature


@pytest.mark.fast
def test_sign_request_differs_for_a_different_timestamp() -> None:
    signature = sign_request(KEY, IDEMPOTENCY_KEY, TIMESTAMP)

    assert sign_request(KEY, IDEMPOTENCY_KEY, TIMESTAMP + 1) != signature


@pytest.mark.fast
def test_verify_signature_accepts_a_correctly_signed_request() -> None:
    signature = sign_request(KEY, IDEMPOTENCY_KEY, TIMESTAMP)

    assert verify_signature(KEY, IDEMPOTENCY_KEY, TIMESTAMP, signature) is True


@pytest.mark.fast
@pytest.mark.parametrize(
    "bad_signature",
    ["", "0" * 64, "not-hex", sign_request(KEY, IDEMPOTENCY_KEY, TIMESTAMP)[:-1]],
)
def test_verify_signature_rejects_an_incorrect_signature(bad_signature: str) -> None:
    assert verify_signature(KEY, IDEMPOTENCY_KEY, TIMESTAMP, bad_signature) is False


@pytest.mark.fast
def test_verify_signature_rejects_a_signature_signed_with_a_different_key() -> None:
    signature = sign_request(b"a-different-key", IDEMPOTENCY_KEY, TIMESTAMP)

    assert verify_signature(KEY, IDEMPOTENCY_KEY, TIMESTAMP, signature) is False


@pytest.mark.fast
@pytest.mark.parametrize("offset_seconds", [0, 60, -60, 300, -300])
def test_is_fresh_accepts_timestamps_within_the_replay_window(
    offset_seconds: int,
) -> None:
    assert is_fresh(TIMESTAMP + offset_seconds, NOW) is True


@pytest.mark.fast
@pytest.mark.parametrize("offset_seconds", [301, -301, 3600, -3600])
def test_is_fresh_rejects_timestamps_outside_the_replay_window(
    offset_seconds: int,
) -> None:
    assert is_fresh(TIMESTAMP + offset_seconds, NOW) is False


@pytest.mark.fast
def test_is_fresh_uses_the_provided_clock_not_wall_clock() -> None:
    far_future = NOW + timedelta(days=365)

    assert is_fresh(TIMESTAMP, far_future) is False

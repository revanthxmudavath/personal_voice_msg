from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from personal_voice_msg.audio_pipeline import convert_to_opus, synthesize_to_wav
from personal_voice_msg.config import Settings, load_settings
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.sender import (
    RECONCILE_GRACE_SECONDS,
    reconcile_delivery,
    send_voice_note,
    sign_request,
)
from personal_voice_msg.voice_enrollment import enroll_voice

pytestmark = pytest.mark.e2e

VOICE_SAMPLE_ENV = "T13_VOICE_SAMPLE"
WAHA_SETTINGS_ENV = "T15_WAHA_SETTINGS"
_MISSING = [
    name
    for name in (VOICE_SAMPLE_ENV, WAHA_SETTINGS_ENV)
    if name not in os.environ
]
if _MISSING:
    pytestmark = [
        pytest.mark.e2e,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample and a real "
                f"paired WAHA session; set {', '.join(_MISSING)} "
                "(docs/task-logs/T15.md)"
            )
        ),
    ]


def new_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "assistant.sqlite3")
    database.migrate()
    return database


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings(Path(os.environ[WAHA_SETTINGS_ENV]))


@pytest.fixture(scope="module")
def valid_audio_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Real Pocket TTS synthesis + real FFmpeg conversion, once per module."""

    workdir = tmp_path_factory.mktemp("t16_audio")
    raw_sample = workdir / "raw_sample.wav"
    shutil.copyfile(Path(os.environ[VOICE_SAMPLE_ENV]), raw_sample)
    embedding = workdir / "voice_embedding.safetensors"
    enroll_voice(raw_sample, embedding)

    wav_path = workdir / "synthesized.wav"
    synthesize_to_wav(
        embedding,
        "This is a real end to end test of reconciliation against WAHA.",
        wav_path,
    )
    ogg_path = workdir / "synthesized.ogg"
    convert_to_opus(wav_path, ogg_path)
    return ogg_path.read_bytes()


def signed_request(
    settings: Settings, idempotency_key: str, now: datetime
) -> tuple[int, str]:
    timestamp = int(now.timestamp())
    signature = sign_request(
        settings.sender_auth_key.reveal().encode(), idempotency_key, timestamp
    )
    return timestamp, signature


# Outer bound a caller-side reconciliation retry loop uses in this test.
#
# Widened from an initial 30.0 to 40.0 based on real measured data, not a
# guess: 5 consecutive individual real runs of this test (timed with
# `--durations=0`, isolating the "call" phase from fixture setup) against
# the real live session showed a heavy-tailed indexing-lag distribution --
# 3/5 resolved in ~1.5-2.0s, but 2/5 did not resolve within the old 30s
# budget at all (measured "call" durations of 31.62s and 33.01s with the
# outcome still DELIVERY_UNKNOWN at that point). A direct chat-history
# check afterward confirmed both of those messages *did* eventually get
# indexed by WAHA -- this is lag, not data loss -- but this task's own
# tooling could not pin down exactly how much beyond ~33s each one took,
# since the test itself stopped polling at the budget. 40.0 gives ~7-9s of
# real empirical margin over the two measured failures while staying
# meaningfully under RECONCILE_GRACE_SECONDS (45s, sender.py) so a genuine
# regression in reconcile_delivery still fails loudly (either as this
# test's own DELIVERY_UNKNOWN timeout, or as an AUDIO_READY mismatch if
# reconcile_delivery's own internal grace-period clock independently
# elapses first) rather than silently "passing" by accident.
#
# This widening reduces observed test flakiness but does NOT eliminate the
# underlying uncertainty: the true tail of WAHA's indexing-lag distribution
# is not fully characterized by this task's sample size, and there is a
# real, currently undocumented-elsewhere risk that RECONCILE_GRACE_SECONDS
# itself (45s, a production constant, not touched by this change) could be
# too tight under real-world lag spikes -- see docs/task-logs/T16.md's
# "Task 7 fix" section for the full data and why this was flagged rather
# than silently resolved by changing that production constant.
_CALLER_RETRY_BUDGET_SECONDS = 40.0
_CALLER_RETRY_INTERVAL_SECONDS = 2.0


def test_reconcile_delivery_finds_a_message_that_was_actually_sent(
    settings: Settings, valid_audio_bytes: bytes, tmp_path: Path
) -> None:
    """reconcile_delivery's own internal poll only smooths a small,
    common indexing lag (see RECONCILE_POLL_ATTEMPTS in sender.py) -- it
    is not sized to guarantee finding a just-sent message in one call.
    This test instead plays the role of reconcile_delivery's documented
    caller (Task 8's orchestrator): call it, and if it answers
    DELIVERY_UNKNOWN, call it again shortly, exactly as the function's own
    docstring instructs callers to do -- bounded so a genuine failure to
    ever find the message still fails the test instead of silently
    passing once the grace period elapses.
    """

    database = new_database(tmp_path)
    now = datetime.now(UTC)
    idempotency_key = f"t16-reconcile-sent-{now.timestamp()}"
    timestamp, signature = signed_request(settings, idempotency_key, now)
    attempt_window_start = now

    async def send_then_reconcile() -> tuple[MessageState, str | None, str]:
        async with aiohttp.ClientSession() as session:
            provider_message_id = await send_voice_note(
                session,
                database,
                settings,
                valid_audio_bytes,
                idempotency_key,
                timestamp,
                signature,
                now,
            )
            deadline = time.monotonic() + _CALLER_RETRY_BUDGET_SECONDS
            outcome, found_id = await reconcile_delivery(
                session, settings, attempt_window_start, datetime.now(UTC)
            )
            while (
                outcome is MessageState.DELIVERY_UNKNOWN
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(_CALLER_RETRY_INTERVAL_SECONDS)
                outcome, found_id = await reconcile_delivery(
                    session, settings, attempt_window_start, datetime.now(UTC)
                )
            return outcome, found_id, provider_message_id

    outcome, found_id, provider_message_id = asyncio.run(send_then_reconcile())

    assert outcome is MessageState.SENT
    assert found_id == provider_message_id


def test_reconcile_delivery_reports_not_delivered_for_a_window_with_no_send(
    settings: Settings, tmp_path: Path
) -> None:
    """No send happens in this test's own window. The window must start
    at this test's own invocation time, not further in the past -- the
    real chat history is shared and persistent across test runs (and
    other tests in this file do real sends), so a window reaching back
    minutes would risk catching an unrelated real message and asserting
    the wrong outcome for the wrong reason. Grace-period elapse is
    simulated via the ``now`` argument (well past RECONCILE_GRACE_SECONDS)
    without actually sleeping that long.
    """

    attempt_window_start = datetime.now(UTC)

    async def reconcile() -> tuple[MessageState, str | None]:
        async with aiohttp.ClientSession() as session:
            return await reconcile_delivery(
                session,
                settings,
                attempt_window_start,
                attempt_window_start + timedelta(seconds=RECONCILE_GRACE_SECONDS + 5),
            )

    outcome, found_id = asyncio.run(reconcile())

    assert outcome is MessageState.AUDIO_READY
    assert found_id is None


def test_reconcile_delivery_is_inconclusive_within_the_grace_period(
    settings: Settings, tmp_path: Path
) -> None:
    """No matching send, but barely any time has elapsed since the window
    opened -- WAHA's own history may simply not have caught up yet, so
    this must stay DELIVERY_UNKNOWN rather than be declared AUDIO_READY.
    """

    now = datetime.now(UTC)
    attempt_window_start = now

    async def reconcile() -> tuple[MessageState, str | None]:
        async with aiohttp.ClientSession() as session:
            return await reconcile_delivery(
                session,
                settings,
                attempt_window_start,
                now + timedelta(seconds=1),
            )

    outcome, found_id = asyncio.run(reconcile())

    assert outcome is MessageState.DELIVERY_UNKNOWN
    assert found_id is None

# T17b — Daily-send entrypoint and live STOP wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `poll_inbound_stop` and `run_daily_send` their first real caller — a
minimal, short-lived entrypoint function plus the runnable script that invokes it on
a cron tick — and hand the owner the exact steps to run live verification against
real Telegram infrastructure outside this sandbox.

**Architecture:** One new function, `run_daily_entrypoint`, in a new
`daily_send_entrypoint.py` module: it checks whether the `DAILY_SEND` trigger is
currently due (a pure no-op otherwise), polls once for an inbound STOP (discarding
poll failures so they never block a send), then delegates to the existing
`run_daily_send`. One new runnable script, `scripts/run_daily_entrypoint.py`, wires
real settings/database/session into that function and prints the result. Nothing in
`consent.py`, `delivery.py`, or `sender.py` changes.

**Tech Stack:** Python 3.12, `aiohttp`, `argparse`, `pytest` — no new dependencies.

## Global Constraints

- No mocks: real file-backed SQLite, real local sockets for HTTP fault injection,
  real Telegram-shaped payloads served locally — `AGENTS.md` §Strict no-mock TDD
  policy.
- Fail closed: an unhandled exception from the script propagates and exits non-zero;
  no swallowed errors beyond the one documented `TelegramPollError` case.
- Short-lived, not a daemon: no internal sleep loop, no process supervision — see
  the design spec's "Entrypoint shape" section.
- Secrets (bot token, chat id, voice embedding) never in Git, logs, or task prompts.
- This task loads real secrets, derives real production identifiers, and drives a
  real send through a new production-facing entry surface — not on `AGENTS.md`'s
  original mandatory-review list, but reviewed anyway per the T16b precedent (Task 4
  requests independent review; do not self-approve).
- Design reference:
  `docs/superpowers/specs/2026-08-20-t17b-daily-send-entrypoint-design.md`.

---

### Task 1: The entrypoint function — window gating, STOP-then-send

**Files:**
- Create: `src/personal_voice_msg/daily_send_entrypoint.py`
- Test: `tests/fast/test_daily_send_entrypoint.py` (new)

**Interfaces:**
- Consumes: `personal_voice_msg.consent.poll_inbound_stop`,
  `personal_voice_msg.consent.TelegramPollError`,
  `personal_voice_msg.delivery.run_daily_send`,
  `personal_voice_msg.scheduling.{ScheduleKind, TriggerStatus, classify_trigger,
  planned_triggers_for_date}`, `personal_voice_msg.sender.TELEGRAM_API_BASE`,
  `personal_voice_msg.database.{Database, MessageState}`,
  `personal_voice_msg.config.Settings` — all exactly as they exist on this branch
  today (re-verified against current source; see the design spec's "Exact
  interfaces consumed" section).
- Produces: `async def run_daily_entrypoint(database: Database, settings: Settings,
  session: aiohttp.ClientSession, recipient_key: str, pacific_date: date,
  embedding_path: Path, now: datetime, *, api_base: str = TELEGRAM_API_BASE) ->
  MessageState | None`. Consumed by Task 2 (fault-injection tests) and Task 3 (the
  script).

- [ ] **Step 1: Write the failing tests**

Create `tests/fast/test_daily_send_entrypoint.py`:

```python
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from personal_voice_msg.daily_send_entrypoint import run_daily_entrypoint
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.scheduling import ScheduleKind, planned_triggers_for_date


def _send_trigger_bounds(pacific_date: date) -> tuple[datetime, datetime]:
    trigger = next(
        t
        for t in planned_triggers_for_date(pacific_date)
        if t.kind is ScheduleKind.DAILY_SEND
    )
    return trigger.scheduled_at, trigger.cutoff_at


@pytest.mark.fast
def test_run_daily_entrypoint_before_the_window_is_a_pure_noop(
    tmp_path: Path,
) -> None:
    """session=None/settings=None proves no DB write and no network call is
    ever attempted outside the window -- a correct implementation that
    tried either would raise before reaching the assertion below."""
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    start, _ = _send_trigger_bounds(pacific_date)
    too_early = start - timedelta(seconds=1)

    async def call() -> MessageState | None:
        return await run_daily_entrypoint(
            database, None, None, "recipient_t17b_window",  # type: ignore[arg-type]
            pacific_date, Path("unused"), too_early,
        )

    result = asyncio.run(call())

    assert result is None


@pytest.mark.fast
def test_run_daily_entrypoint_at_or_after_the_cutoff_is_a_pure_noop(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    pacific_date = date(2026, 8, 9)
    _, cutoff = _send_trigger_bounds(pacific_date)

    async def call() -> MessageState | None:
        return await run_daily_entrypoint(
            database, None, None, "recipient_t17b_window",  # type: ignore[arg-type]
            pacific_date, Path("unused"), cutoff,
        )

    result = asyncio.run(call())

    assert result is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/fast/test_daily_send_entrypoint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named
'personal_voice_msg.daily_send_entrypoint'` — the intended reason (module doesn't
exist yet), not an unrelated import or fixture error.

- [ ] **Step 3: Write the minimal implementation**

Create `src/personal_voice_msg/daily_send_entrypoint.py`:

```python
"""Minimal daily-send entrypoint -- gives poll_inbound_stop and
run_daily_send their first real caller. See
docs/superpowers/specs/2026-08-20-t17b-daily-send-entrypoint-design.md.

A short-lived function, not a daemon: it does whatever's due, once, and
returns. scripts/run_daily_entrypoint.py is the process an external timer
(cron inside the container, or a systemd timer -- T18's concern) invokes
every 1-2 minutes; nothing here loops or sleeps.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.config import Settings
from personal_voice_msg.consent import TelegramPollError, poll_inbound_stop
from personal_voice_msg.database import Database, MessageState
from personal_voice_msg.delivery import run_daily_send
from personal_voice_msg.scheduling import (
    ScheduleKind,
    TriggerStatus,
    classify_trigger,
    planned_triggers_for_date,
)
from personal_voice_msg.sender import TELEGRAM_API_BASE


async def run_daily_entrypoint(
    database: Database,
    settings: Settings,
    session: aiohttp.ClientSession,
    recipient_key: str,
    pacific_date: date,
    embedding_path: Path,
    now: datetime,
    *,
    api_base: str = TELEGRAM_API_BASE,
) -> MessageState | None:
    """Advance today's delivery by one step, after giving a pending STOP a
    chance to take effect first. Returns ``None`` -- touching neither the
    database nor the network -- outside the DAILY_SEND window, so this is
    safe to call on every cron tick all day.
    """

    send_trigger = next(
        trigger
        for trigger in planned_triggers_for_date(pacific_date)
        if trigger.kind is ScheduleKind.DAILY_SEND
    )
    if classify_trigger(send_trigger, now) is not TriggerStatus.DUE:
        return None

    try:
        await poll_inbound_stop(session, database, settings, now, api_base=api_base)
    except TelegramPollError:
        # Poll fragility must never block a legitimate send attempt -- see
        # the design spec's "The entrypoint function" section. No
        # structured logging exists yet (T19), so this is deliberately a
        # bare pass, not dressed up as more than it is.
        pass

    return await run_daily_send(
        database,
        settings,
        session,
        recipient_key,
        pacific_date,
        embedding_path,
        now,
        api_base=api_base,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/fast/test_daily_send_entrypoint.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the fast regression suite**

Run: `uv run pytest -m fast -q`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add src/personal_voice_msg/daily_send_entrypoint.py tests/fast/test_daily_send_entrypoint.py
git commit -m "T17b: add the daily-send entrypoint function"
```

---

### Task 2: Fault-injection proof — STOP-vs-send ordering, poll-failure resilience

**Files:**
- Test: `tests/security/test_daily_send_entrypoint_fault_injection.py` (new)

**Interfaces:**
- Consumes: `run_daily_entrypoint` (Task 1, exact signature above);
  `personal_voice_msg.database.{Database, MessageState, recipient_key_for_chat_id}`;
  `personal_voice_msg.history.MessageHistory`; `personal_voice_msg.config.{Settings,
  RuntimeProfile}`; `personal_voice_msg.redaction.SensitiveValue`;
  `personal_voice_msg.voice_enrollment.enroll_voice`; `personal_voice_msg.scheduling.
  {ScheduleKind, planned_triggers_for_date}`.
- Produces: nothing consumed by later tasks — this is a leaf verification task.

This reuses the established local fake-server pattern from
`tests/security/test_sender_error_taxonomy.py` (`_FixedStatusServer`) and
`tests/e2e/test_delivery_fault_injection.py`, extended with path-based routing since
one call now makes two different real HTTP calls (`getUpdates` then `sendVoice`)
against the same `api_base`. Gated on `T13_VOICE_SAMPLE` only (a real consented
voice sample, needed so `produce_voice_note`'s real TTS + FFmpeg pipeline has
something real to synthesize) — no real Telegram credentials, matching the design
spec's "nothing here needs real Telegram credentials."

- [ ] **Step 1: Write the failing tests**

Create `tests/security/test_daily_send_entrypoint_fault_injection.py`:

```python
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

if VOICE_SAMPLE_ENV not in os.environ:
    pytestmark = [
        pytest.mark.security,
        pytest.mark.skip(
            reason=(
                "requires a real consented test voice sample so "
                "produce_voice_note's real TTS/FFmpeg pipeline has "
                f"something real to synthesize; set {VOICE_SAMPLE_ENV}"
            )
        ),
    ]


class _RoutingServer:
    """Accepts connections one at a time, in a loop, until stopped.
    Drains each request until the connection goes quiet (matching
    _FixedStatusServer's established pattern in
    tests/security/test_sender_error_taxonomy.py -- a real raw socket, no
    aiohttp/Telegram server semantics beyond the status line, headers,
    and body), extracts the request path from the first line, and
    responds with whichever of ``routes`` matches a substring of that
    path. Records every path seen, in order, so a test can assert not
    just what each endpoint returned but the order -- or absence -- of
    the calls that reached it.
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
            except TimeoutError:
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
                self.paths_seen.append(path)
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
    embedding_path: Path, tmp_path: Path
) -> None:
    """The offset-cursor poll happens before run_daily_send re-reads
    is_sending_enabled -- a STOP arriving in this call's own poll must
    already have taken effect by the time the send would otherwise be
    attempted, proven here by the sendVoice route never being hit."""
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
```

- [ ] **Step 2: Run the tests to verify they fail (or skip) for the intended reason**

Run: `uv run pytest tests/security/test_daily_send_entrypoint_fault_injection.py -v`
Expected: without `T13_VOICE_SAMPLE` set, all 3 tests SKIP with the documented
reason. With it set (Task 1 already implemented), they should already PASS since
Task 1's implementation is correct by construction — if any FAIL, read the failure
against Task 1's actual source before changing anything (this task's tests must not
themselves have latent bugs from being written before being run).

- [ ] **Step 3: Run the tests to verify they pass with the voice sample set**

Run: `T13_VOICE_SAMPLE=<path-to-a-real-consented-wav> uv run pytest
tests/security/test_daily_send_entrypoint_fault_injection.py -v`
Expected: PASS (3 passed) — run this only if a real consented sample is available in
the current environment; otherwise record the skip and defer this confirmation to
Task 5's live-verification handoff.

- [ ] **Step 4: Run the fast and security regression suites**

Run: `uv run pytest -m fast -q && uv run pytest -m security -q`
Expected: PASS, no new failures (env-gated tests skip cleanly without credentials).

- [ ] **Step 5: Commit**

```bash
git add tests/security/test_daily_send_entrypoint_fault_injection.py
git commit -m "T17b: prove STOP-vs-send ordering and poll-failure resilience"
```

---

### Task 3: The runnable script

**Files:**
- Create: `scripts/run_daily_entrypoint.py`
- Test: `tests/fast/test_run_daily_entrypoint_script.py` (new)

**Interfaces:**
- Consumes: `run_daily_entrypoint` (Task 1); `personal_voice_msg.config.
  load_settings`; `personal_voice_msg.database.{Database,
  recipient_key_for_chat_id}`; `personal_voice_msg.scheduling.PACIFIC`.
- Produces: nothing consumed by later tasks.

Following this repo's one existing script precedent
(`scripts/repository_policy.py`): `argparse`, a `if __name__ == "__main__":` block,
tested via `subprocess` (see `tests/fast/test_repository_policy.py`). The script's
actual runtime behavior against a real window and real credentials is verified live
by the owner in Task 5, not simulated here with a fabricated `now` — this task's
automated test covers only the deterministic, wall-clock-independent part: the CLI
contract.

- [ ] **Step 1: Write the failing tests**

Create `tests/fast/test_run_daily_entrypoint_script.py`:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "run_daily_entrypoint.py"


@pytest.mark.fast
def test_run_daily_entrypoint_script_requires_config_and_database() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "--config" in result.stderr
    assert "--database" in result.stderr


@pytest.mark.fast
def test_run_daily_entrypoint_script_requires_database_when_only_config_given(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(tmp_path / "settings.toml")],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "--database" in result.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/fast/test_run_daily_entrypoint_script.py -v`
Expected: FAIL — `SCRIPT` path does not exist yet, so `subprocess.run` raises
`FileNotFoundError`.

- [ ] **Step 3: Write the minimal implementation**

Create `scripts/run_daily_entrypoint.py`:

```python
"""Runnable entrypoint for the daily-send process: run one tick, do
whatever's due (poll for a STOP, then advance today's delivery), and
exit. An external timer (cron inside the container, or a systemd timer --
T18's concern, not this script's) invokes this every 1-2 minutes; nothing
here loops or sleeps.

usage: run_daily_entrypoint.py --config CONFIG --database DATABASE
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from personal_voice_msg.config import load_settings
from personal_voice_msg.daily_send_entrypoint import run_daily_entrypoint
from personal_voice_msg.database import Database, recipient_key_for_chat_id
from personal_voice_msg.scheduling import PACIFIC


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one tick of the daily-send entrypoint: poll for a STOP "
            "command, then advance today's delivery by one step if the "
            "daily-send window is currently open."
        )
    )
    parser.add_argument(
        "--config", type=Path, required=True,
        help="path to a load_settings-compatible TOML configuration file",
    )
    parser.add_argument(
        "--database", type=Path, required=True,
        help="path to the SQLite state file",
    )
    return parser.parse_args()


async def _run(config_path: Path, database_path: Path) -> None:
    settings = load_settings(config_path)
    recipient_key = recipient_key_for_chat_id(settings.telegram_chat_id.reveal())
    embedding_path = settings.voice_embedding.reveal()
    database = Database(database_path)
    database.migrate()
    now = datetime.now(UTC)
    pacific_date = now.astimezone(PACIFIC).date()

    async with aiohttp.ClientSession() as session:
        result = await run_daily_entrypoint(
            database, settings, session, recipient_key, pacific_date,
            embedding_path, now,
        )

    print("not due, skipped" if result is None else result.value)


def main() -> None:
    args = parse_args()
    asyncio.run(_run(args.config, args.database))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/fast/test_run_daily_entrypoint_script.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the fast regression suite, ruff, and mypy**

```bash
uv run pytest -m fast -q
uv run ruff check .
uv run mypy src
```

Expected: all PASS/clean.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_daily_entrypoint.py tests/fast/test_run_daily_entrypoint_script.py
git commit -m "T17b: add the runnable daily-send entrypoint script"
```

---

### Task 4: Full verification, independent review, and IMPLEMENTATION_PLAN.md insertion

**Interfaces:** None — documentation, verification, and review only.

- [ ] **Step 1: Run the complete local verification suite fresh, in order**

```bash
uv run pytest -m fast -q
uv run pytest -m integration -q
uv run pytest -m security -q
uv run ruff check .
uv run mypy src
uv run python scripts/repository_policy.py all --root .
```

Record the exact pass/fail counts and any env-gated skips (matching T16b's and
T17's own verification section style in `docs/task-logs/T16b.md` /
`docs/task-logs/T17.md`).

- [ ] **Step 2: Request the independent whole-branch review**

Per this project's T16b precedent (not on `AGENTS.md`'s original mandatory list, but
reviewed anyway because it loads real secrets, derives real production identifiers,
and drives a real send through a new production-facing entry surface): dispatch a
fresh, unbiased reviewer (no prior context from this implementation) to trace the
actual source of `daily_send_entrypoint.py` and `scripts/run_daily_entrypoint.py`
against:
- the design spec
  (`docs/superpowers/specs/2026-08-20-t17b-daily-send-entrypoint-design.md`)
- this plan's tasks and their tests

Fix any findings the reviewer confirms against current source (not this plan's
prose) before proceeding. Record the review outcome in `docs/task-logs/T17b.md`
(Step 4 below).

- [ ] **Step 3: Insert the T17b section into `IMPLEMENTATION_PLAN.md`**

In `IMPLEMENTATION_PLAN.md` §9 ("Executable task backlog"), insert a new
`### T17b — Daily-send entrypoint and live STOP wiring` section between the
existing `### T17` section and the existing `### T18` section, in the same style as
the existing `### T16b` section (Dependencies line, red tests, implementation
bullets, done-when sentence). Then edit T18's existing `Dependencies: T06, T15, T17`
line to `Dependencies: T06, T15, T17, T17b`. Do not touch AGENTS.md's simplified
"Backlog order" list or IMPLEMENTATION_PLAN.md §10's dependency-summary diagram —
T16b was inserted the same way (own section only, those two summaries left as-is);
matching that precedent keeps this task's scope narrow.

- [ ] **Step 4: Commit**

```bash
git add IMPLEMENTATION_PLAN.md
git commit -m "T17b: insert plan section, add T18 dependency, record review"
```

---

### Task 5: Live verification handoff (owner-run, outside this sandbox)

**Interfaces:** None — this task is executed by the controlling session pausing to
hand off instructions to the owner, not by a dispatched subagent (the sandboxed
session cannot reach `api.telegram.org` over genuine TLS — see the design spec's
"Live verification" section).

- [ ] **Step 1: Give the owner the one-time recipient enrollment command**

Present this in chat for the owner to run in their own terminal, outside the
sandbox (only if not already enrolled from T16b/T17's own live verification):

```bash
uv run python -c "
from pathlib import Path
from personal_voice_msg.recipient_enrollment import enroll_recipient
from personal_voice_msg.config import RuntimeProfile
enroll_recipient('<real-bot-token>', Path('<real-chat-id-output-path>'), RuntimeProfile.DEVELOPMENT)
"
```

- [ ] **Step 2: Give the owner the exact env-gated test invocations**

```bash
T13_VOICE_SAMPLE=<path-to-a-real-consented-wav> uv run pytest tests/security/test_daily_send_entrypoint_fault_injection.py -v
T13_VOICE_SAMPLE=<path> T16B_TELEGRAM_SETTINGS=<path-to-real-settings-toml> T16B_TEST_BOT_TOKEN=<real-bot-token> uv run pytest -m integration -v
T13_VOICE_SAMPLE=<path> T16B_TELEGRAM_SETTINGS=<path> T16B_TEST_BOT_TOKEN=<real-bot-token> uv run pytest -m e2e -v
```

- [ ] **Step 3: Give the owner the exact real-script invocation**

```bash
uv run python scripts/run_daily_entrypoint.py --config <real-settings-toml> --database <real-state-db>
```

To be run once, for real, during an actual 07:00–07:05 Pacific `DAILY_SEND` window
(check `IMPLEMENTATION_PLAN.md`/`scheduling.py` for the exact configured window if
it differs).

- [ ] **Step 4: Wait for the owner to report results, then record them honestly**

Do not proceed to Task 6 until the owner reports back. Record the actual outcome —
including any failure — in `docs/task-logs/T17b.md`, the same policy this project
has followed since T10.

---

### Task 6: Task log, AGENTS.md update, and PR

**Interfaces:** None — documentation and merge only.

- [ ] **Step 1: Write `docs/task-logs/T17b.md`**

Follow the existing task-log format (see `docs/task-logs/T17.md` for the most
recent example): Status, Design summary (link the design spec), Implementation
(task-by-task commit list), Verification (Task 4 Step 1's command output),
independent review findings and fix round if any (Task 4 Step 2), live verification
results as reported by the owner (Task 5 Step 4), Next step.

- [ ] **Step 2: Update `AGENTS.md`**

In the "Immediate next step" section, replace the current T17-pending-merge
language with a completion entry for both T17 (now merged, per `f577caa`) and T17b,
mirroring T16b's own entry shape — summarize the entrypoint function, the script,
the fault-injection proof, and the live verification outcome, then point to
`docs/task-logs/T17.md` and `docs/task-logs/T17b.md`. Update the "Actual next step"
line to point at T18.

- [ ] **Step 3: Commit**

```bash
git add docs/task-logs/T17b.md AGENTS.md
git commit -m "T17b: record verification, review, and live outcome"
```

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin HEAD
gh pr create --title "T17b: daily-send entrypoint and live STOP wiring" --body "$(cat <<'EOF'
Gives poll_inbound_stop and run_daily_send their first real caller: a minimal
run_daily_entrypoint function plus scripts/run_daily_entrypoint.py, the process an
external timer invokes on a cron tick. Per
docs/superpowers/specs/2026-08-20-t17b-daily-send-entrypoint-design.md and
docs/superpowers/plans/2026-08-20-t17b-daily-send-entrypoint.md.

Independent review and live verification: see docs/task-logs/T17b.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Then, once green and reviewed: `gh pr merge --merge --delete-branch`.

---

## Self-Review

**Spec coverage:**
- Entrypoint shape (short-lived, no daemon) → Task 1's implementation and docstring;
  Global Constraints.
- Exact interfaces re-verified against current source → confirmed directly against
  `delivery.py`, `consent.py`, `scheduling.py`, `config.py`, `database.py` before
  writing this plan (not recalled); Task 1's "Consumes" block.
- `run_daily_entrypoint` window-gating, STOP-then-send, poll-error swallowed → Task
  1 (fast no-op tests) + Task 2 (fault-injection order/STOP/poll-error tests).
- The runnable script's exact CLI shape (`--config`/`--database`) → Task 3.
- Testing plan's fast/security split → Task 1 (fast, wall-clock-independent window
  logic) and Task 2 (security, local fake server, no real credentials).
- Live verification (enrollment, env-gated pytest invocations, real script
  invocation) all owner-run outside the sandbox → Task 5.
- Independent review (not on the original fixed list, required anyway) → Task 4
  Step 2.
- `IMPLEMENTATION_PLAN.md` insertion between T17/T18, T18 gains a T17b dependency →
  Task 4 Step 3.
- "What does NOT change" (no edits to `consent.py`/`delivery.py`/`sender.py`/
  `database.py`) → true throughout; no task touches those files.

**Placeholder scan:** No TBD/TODO; every code step has complete, runnable code.
Task 5's commands use angle-bracket placeholders (`<real-bot-token>`, etc.)
deliberately — they are owner-run commands whose real values are secrets that must
never appear in this plan or in Git, not implementation code needing to be filled
in later.

**Type consistency:** `run_daily_entrypoint`'s signature (Task 1) is used
identically in Task 2's tests and Task 3's script — same parameter names, same
`api_base` keyword-only override, same `MessageState | None` return type. `_settings`
and `_approved_message_in_window` (Task 2) are self-contained to that file, not
consumed elsewhere.

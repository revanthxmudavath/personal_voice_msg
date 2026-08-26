# T18 — Cloud and Container Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and really verify (via Docker Desktop, no mocks) the container, network, secret-permission, and secret-scanning hardening this project needs before a real cloud deployment, plus infra-as-code (firewall/WireGuard/SSH) and a runbook for the owner to apply it to a real VPS later.

**Architecture:** One shared Docker image (`personal-voice-msg`) run as two `docker-compose.yml` services — `app` (generation/judging/voice/sender/delivery/scheduler, in-process, cron-driven) and `discovery` (a bounded verification-harness worker) — on separate non-IPv6 networks with per-service secret mounts, capability drops, read-only root filesystems, and resource limits. Deployment-only pieces (real WireGuard-only public firewall, real external port scan) ship as reviewed infra-as-code plus a runbook, verified locally against a throwaway Linux container's real `nft` ruleset rather than a live VPS.

**Tech Stack:** Docker Compose, nftables, WireGuard, supercronic (non-root cron), Python 3.12/aiohttp (existing), pytest (existing `fast`/`integration`/`security` markers).

## Global Constraints

- No mocks anywhere — real containers, real `docker exec`/`docker inspect`, real network fault injection (stop containers, apply real firewall rules, exhaust real resource limits), real file permissions inside a real Linux container. (`AGENTS.md` §Strict no-mock TDD policy)
- Fail closed on unknown secret/permission/network state. (`AGENTS.md` §Content and rights rules pattern, extended here to secrets)
- One backlog task in progress at a time; no unrelated refactoring; every changed line traces to T18. (`CLAUDE.md`)
- Pin dependency versions and container digests; any managed-API pin change requires requalification. (`AGENTS.md` §Network and container rules)
- Secrets never in Git, logs, images, or command args. (`AGENTS.md` §Voice and privacy rules)
- Branch: `task/T18-cloud-container-hardening`. Commit form: `T18: concise verified outcome`. PR + `gh pr merge --merge --delete-branch`, matching this repo's history.
- Independent security review required before merge (`AGENTS.md`'s fixed T06/T15/T16/T17/T18 review list) — do not self-approve.
- Design reference: `docs/superpowers/specs/2026-08-24-t18-cloud-container-hardening-design.md`. Read it before starting — this plan implements it task-by-task and does not repeat its rationale.

---

## Task 1: Secret file ownership and mode validation

**Files:**
- Modify: `src/personal_voice_msg/config.py`
- Test: `tests/security/test_secret_file_permissions.py` (new)

**Interfaces:**
- Consumes: `personal_voice_msg.config.ConfigurationError` (existing), `secret_file()` (existing, at `config.py`'s current `secret_file(root: Path, value: str, setting: str) -> Path`).
- Produces: `secret_file()` now also raises `ConfigurationError` for a non-development-profile secret file that is not owned by the running effective UID, or has any group/other permission bit set. No new public names — this is a behavior change to an existing function's fail-closed checks.

Windows has no POSIX mode bits, so this must run inside a real Linux container (Docker Desktop is confirmed working). The test builds a minimal throwaway container, writes a secret file into a bind-mounted temp dir from **inside** the container (so the file's owner/mode are real Linux semantics, not whatever NTFS reports), `chmod`s/leaves-permissive it for real, then runs the actual `personal_voice_msg.config` code inside that same container via `docker exec ... python -c ...` against the installed editable package.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_secret_file_permissions.py
from __future__ import annotations

import subprocess
import uuid

import pytest

pytestmark = pytest.mark.security

IMAGE = "python:3.12-slim"


def _run_check(mode: str, owner_uid: int, check_uid: int) -> subprocess.CompletedProcess[str]:
    """Create a secret file with the given mode/owner inside a real Linux
    container, then run the real permission-check function as `check_uid`
    against it, entirely inside that container. Returns the completed
    process; stdout is "OK" or "REJECTED: <message>".
    """
    container = f"t18-secret-perm-{uuid.uuid4().hex[:8]}"
    project_root = "/workspace"
    script = f"""
import os, pwd, grp
os.makedirs('/secrets', exist_ok=True)
uid = {owner_uid}
# Ensure a user with this uid exists (root=0 always does); create one otherwise.
try:
    pwd.getpwuid(uid)
except KeyError:
    os.system(f"useradd -u {{uid}} -M owner{{uid}}")
path = '/secrets/token.txt'
with open(path, 'w') as f:
    f.write('secret-value\\n')
os.chown(path, uid, uid)
os.chmod(path, {mode})

os.setuid({check_uid})
import sys
sys.path.insert(0, '/workspace/src')
from personal_voice_msg.config import ConfigurationError, secret_file
from pathlib import Path
try:
    secret_file(Path('/secrets'), 'token.txt', 'telegram_bot_token_file')
    print('OK')
except ConfigurationError as exc:
    print(f'REJECTED: {{exc}}')
"""
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", container,
             "-v", f"{__file__.rsplit('tests', 1)[0]}:{project_root}:ro",
             IMAGE, "sleep", "60"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["docker", "exec", "-u", "root", container, "python3", "-c", script],
            check=False, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["docker", "exec", "-u", "root", container, "python3", "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        return result
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def test_owner_only_mode_owned_by_running_uid_is_accepted() -> None:
    result = _run_check(mode="0o600", owner_uid=1000, check_uid=1000)
    assert "OK" in result.stdout, result.stdout + result.stderr


def test_group_readable_secret_file_is_rejected() -> None:
    result = _run_check(mode="0o640", owner_uid=1000, check_uid=1000)
    assert "REJECTED" in result.stdout, result.stdout + result.stderr


def test_world_readable_secret_file_is_rejected() -> None:
    result = _run_check(mode="0o604", owner_uid=1000, check_uid=1000)
    assert "REJECTED" in result.stdout, result.stdout + result.stderr


def test_secret_file_owned_by_a_different_uid_is_rejected() -> None:
    result = _run_check(mode="0o600", owner_uid=1001, check_uid=1000)
    assert "REJECTED" in result.stdout, result.stdout + result.stderr
```

Note: `secret_file()` currently takes `(root, value, setting)` with no `profile`
parameter — the permission check must apply regardless of profile per this
task, OR only to non-development profiles matching `secret_root()`'s existing
exemption pattern. Resolve this in Step 3: **add a `profile` keyword argument
to `secret_file()`, default `RuntimeProfile.PRODUCTION`** so every existing
call site inside `load_settings()` (which already has `profile` in scope)
passes it explicitly, and the four tests above (which call `secret_file`
directly without a profile) get the strict default — matching this file's
existing pattern where `secret_root()` already takes `profile` explicitly.

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose version >/dev/null && uv run pytest tests/security/test_secret_file_permissions.py -v` (Docker must be running — confirmed working this session)
Expected: FAIL — `test_group_readable_secret_file_is_rejected` and
`test_world_readable_secret_file_is_rejected` and
`test_secret_file_owned_by_a_different_uid_is_rejected` print `OK` instead of
`REJECTED` (current `secret_file()` has no permission check at all).

- [ ] **Step 3: Implement the minimal change**

In `src/personal_voice_msg/config.py`, add after the imports:

```python
import os
import stat
```

Replace the existing `secret_file` function:

```python
def secret_file(
    root: Path,
    value: str,
    setting: str,
    *,
    profile: RuntimeProfile = RuntimeProfile.PRODUCTION,
) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ConfigurationError(f"{setting} must be relative to secret root")
    try:
        resolved = (root / relative).resolve(strict=True)
    except OSError:
        raise ConfigurationError(f"{setting} is missing") from None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ConfigurationError(f"{setting} is outside secret root or not a file")
    if profile is not RuntimeProfile.DEVELOPMENT:
        _validate_secret_file_permissions(resolved, setting)
    return resolved


def _validate_secret_file_permissions(path: Path, setting: str) -> None:
    try:
        info = path.stat()
    except OSError:
        raise ConfigurationError(f"{setting} is unreadable") from None
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ConfigurationError(
            f"{setting} is not owned by the running service identity"
        )
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigurationError(f"{setting} has an insecure permission mode")
```

Update `load_settings()`'s four `secret_file(...)` calls to pass
`profile=profile` (the local variable already bound a few lines above each
call site).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/security/test_secret_file_permissions.py -v`
Expected: PASS, all 4 tests.

- [ ] **Step 5: Run the existing configuration regression suite**

Run: `uv run pytest tests/fast/test_configuration.py -v`
Expected: PASS — this suite runs on Windows/NTFS where `os.geteuid` doesn't
exist (`hasattr` guard above no-ops the owner check there) and where mode
bits are meaningless, so it must stay green unchanged. If any existing test
in this file fails, the `profile=profile` threading broke a call site —
fix before proceeding, do not weaken the new check.

- [ ] **Step 6: Commit**

```bash
git add src/personal_voice_msg/config.py tests/security/test_secret_file_permissions.py
git commit -m "T18: fail closed on insecurely-permissioned deployed secret files"
```

---

## Task 2: Discovery worker verification entrypoint

**Files:**
- Create: `src/personal_voice_msg/discovery_worker_entrypoint.py`
- Create: `scripts/run_discovery_worker.py`
- Test: `tests/integration/test_discovery_worker_entrypoint.py` (new)

**Interfaces:**
- Consumes: `personal_voice_msg.discovery.baseline.DeterministicDiscovery`
  (`search_web(query) -> tuple[SearchResult, ...]`,
  `analyze_result(result_id, analyzer) -> DiscoveryRecord`),
  `personal_voice_msg.discovery.baseline.DISCOVERY_QUERIES` (existing tuple
  of 3 fixed query strings), `personal_voice_msg.discovery.web.DiscoveryWebSession`
  (existing).
- Produces: `async def run_discovery_worker(discovery: DeterministicDiscovery, web_session: DiscoveryWebSession, *, wall_clock_budget_seconds: float = 60.0) -> int` — returns the count of `DiscoveryRecord`s successfully analyzed. Deliberately returns only a count, not the records themselves — this is a verification harness (see design spec §4), not a producer of stored candidates.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_discovery_worker_entrypoint.py
from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest

from personal_voice_msg.discovery.baseline import (
    DISCOVERY_QUERIES,
    DeterministicDiscovery,
)
from personal_voice_msg.discovery.web import DiscoveryWebSession, FetchPolicy
from personal_voice_msg.discovery_worker_entrypoint import run_discovery_worker

pytestmark = pytest.mark.integration


import os

if os.environ.get("T07_NETWORK_HARNESS") != "1":
    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skip(reason="requires the isolated T07 Docker network"),
    ]


def test_run_discovery_worker_returns_a_bounded_record_count() -> None:
    """Reuses T07's own real-network test harness (T07_NETWORK_HARNESS=1,
    SEARXNG_URL) rather than inventing a second one -- see
    tests/integration/test_discovery_baseline_network.py for the fixture
    this depends on."""

    async def _run() -> int:
        async with aiohttp.ClientSession() as client:
            web_session = DiscoveryWebSession(client, FetchPolicy())
            discovery = DeterministicDiscovery(
                os.environ["SEARXNG_URL"], web_session
            )
            return await run_discovery_worker(
                discovery, web_session, wall_clock_budget_seconds=45.0
            )

    count = asyncio.run(_run())
    assert count >= 1, "expected at least one real page analyzed from the real SearXNG fixture"


def test_run_discovery_worker_stops_at_the_wall_clock_budget() -> None:
    """No real SearXNG needed for this one: an unreachable endpoint proves
    the budget loop still terminates promptly rather than hanging on
    DISCOVERY_QUERIES's fixed 3-query loop with retries."""

    async def _run() -> tuple[int, float]:
        async with aiohttp.ClientSession() as client:
            web_session = DiscoveryWebSession(client, FetchPolicy())
            discovery = DeterministicDiscovery(
                "http://127.0.0.1:1", web_session
            )
            started = time.monotonic()
            count = await run_discovery_worker(
                discovery, web_session, wall_clock_budget_seconds=5.0
            )
            return count, time.monotonic() - started

    count, elapsed = asyncio.run(_run())
    assert count == 0
    assert elapsed < 15.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_discovery_worker_entrypoint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named
'personal_voice_msg.discovery_worker_entrypoint'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/personal_voice_msg/discovery_worker_entrypoint.py
"""Bounded verification-harness entrypoint for the discovery container.

Reuses T07's already-tested DeterministicDiscovery/DiscoveryWebSession to
give the discovery container a real, bounded, resource-exhaustible process
to run and fault-inject against. Deliberately stops at DiscoveryRecord --
it does not build InspirationCards, does not call generation or judging,
and does not touch the database. Wiring the full weekly production
pipeline (search -> card -> generate -> judge -> queue) is a pre-existing
gap this entrypoint does not solve -- see
docs/superpowers/specs/2026-08-24-t18-cloud-container-hardening-design.md.
"""

from __future__ import annotations

import time

from personal_voice_msg.discovery.baseline import (
    DISCOVERY_QUERIES,
    DeterministicDiscovery,
    DiscoveryExtractionError,
    DiscoverySearchError,
)
from personal_voice_msg.discovery.web import DiscoveryWebSession


def _null_analyzer(text: str, record: object) -> None:
    """Accept every extracted page -- this harness counts successful
    extractions, it does not apply T08's content/rights transformation."""


async def run_discovery_worker(
    discovery: DeterministicDiscovery,
    web_session: DiscoveryWebSession,
    *,
    wall_clock_budget_seconds: float = 60.0,
) -> int:
    started = time.monotonic()
    analyzed = 0
    for query in DISCOVERY_QUERIES:
        if time.monotonic() - started >= wall_clock_budget_seconds:
            break
        try:
            results = await discovery.search_web(query)
        except DiscoverySearchError:
            continue
        for result in results:
            if time.monotonic() - started >= wall_clock_budget_seconds:
                break
            try:
                await discovery.analyze_result(result.result_id, _null_analyzer)
                analyzed += 1
            except DiscoveryExtractionError:
                continue
    return analyzed
```

```python
# scripts/run_discovery_worker.py
"""Runnable entrypoint for the discovery container's default command --
see src/personal_voice_msg/discovery_worker_entrypoint.py for what this
actually does and does not do (verification harness, not the production
weekly pipeline)."""

from __future__ import annotations

import argparse
import asyncio

import aiohttp

from personal_voice_msg.discovery.baseline import DeterministicDiscovery
from personal_voice_msg.discovery.web import DiscoveryWebSession, FetchPolicy
from personal_voice_msg.discovery_worker_entrypoint import run_discovery_worker


async def _main(searxng_base_url: str, budget_seconds: float) -> int:
    async with aiohttp.ClientSession() as client:
        web_session = DiscoveryWebSession(client, FetchPolicy())
        discovery = DeterministicDiscovery(searxng_base_url, web_session)
        return await run_discovery_worker(
            discovery, web_session, wall_clock_budget_seconds=budget_seconds
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--searxng-base-url", required=True)
    parser.add_argument("--budget-seconds", type=float, default=60.0)
    args = parser.parse_args()
    analyzed_count = asyncio.run(_main(args.searxng_base_url, args.budget_seconds))
    print(f"analyzed {analyzed_count} pages")
```

Check `FetchPolicy`'s real constructor signature in
`src/personal_voice_msg/discovery/web.py` before finalizing this step --
adjust the no-arg `FetchPolicy()` calls above to whatever defaults that
dataclass actually requires (read the file first; do not guess).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_discovery_worker_entrypoint.py -v`
Expected: `test_run_discovery_worker_stops_at_the_wall_clock_budget` PASSes
unconditionally. `test_run_discovery_worker_returns_a_bounded_record_count`
is skipped unless `T07_NETWORK_HARNESS=1`/`SEARXNG_URL` are already set in
this environment (T07's own existing real-network harness — check whether
`tests/integration/test_discovery_baseline_network.py` runs unskipped here
first; if it does, this new test should too, using the same env vars, and
must actually PASS, not just run).

- [ ] **Step 5: Commit**

```bash
git add src/personal_voice_msg/discovery_worker_entrypoint.py scripts/run_discovery_worker.py tests/integration/test_discovery_worker_entrypoint.py
git commit -m "T18: add bounded discovery worker verification entrypoint"
```

---

## Task 3: Shared container image (Dockerfile)

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Test: `tests/security/test_container_image.py` (new)

**Interfaces:**
- Consumes: `pyproject.toml`/`uv.lock` (existing, unchanged).
- Produces: a local image tagged `personal-voice-msg:t18-dev` for the rest of this plan's tasks to build on. No Python interfaces — this task is Docker-only.

- [ ] **Step 1: Resolve real pins before writing the Dockerfile**

Run for real, record the output in the Dockerfile as comments (do not guess
these values):

```bash
docker pull python:3.12-slim
docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
```

Also resolve the current supercronic release: fetch
`https://github.com/aptible/supercronic/releases/latest` (via `WebFetch`)
for the current version tag and the matching `SHA256SUMS` asset for the
`linux-amd64` binary. Record both the version and the exact sha256 hex
digest actually published there — do not fabricate a hash.

- [ ] **Step 2: Write the failing test**

```python
# tests/security/test_container_image.py
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.security

IMAGE = "personal-voice-msg:t18-dev"


def test_image_builds() -> None:
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE, "."],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_image_runs_as_a_non_root_fixed_uid() -> None:
    result = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "id", "-u"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    uid = int(result.stdout.strip())
    assert uid != 0


def test_image_has_no_docker_socket_or_cli() -> None:
    which = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "sh", "-c", "command -v docker; ls /var/run/docker.sock"],
        capture_output=True, text=True, timeout=30,
    )
    assert which.returncode != 0
    assert "docker.sock" not in which.stdout


def test_image_has_ffmpeg_and_supercronic() -> None:
    result = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "sh", "-c", "ffmpeg -version && supercronic -version"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/security/test_container_image.py -v`
Expected: FAIL — `docker build` fails, no `Dockerfile` exists.

- [ ] **Step 4: Write the Dockerfile**

```dockerfile
# syntax=docker/dockerfile:1
# Base digest resolved 2026-08-24 -- see Task 3 Step 1 for how to
# re-resolve before any future bump. Requalify (rebuild + rerun this
# task's full test suite) before changing this pin.
FROM python:3.12-slim@sha256:<PASTE THE REAL DIGEST FROM STEP 1> AS builder

RUN pip install --no-cache-dir uv==0.9.7

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.12-slim@sha256:<PASTE THE REAL DIGEST FROM STEP 1> AS runtime

# ffmpeg: required by audio_pipeline.py (T14). Version pinned to whatever
# this base image's apt snapshot actually resolves -- record the resolved
# version here after Step 1's real `apt-get install` run.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# supercronic: non-root-capable cron for the app container's daily-send
# timer (Task 7). Pinned version/digest from Step 1 -- do not use `latest`.
ARG SUPERCRONIC_VERSION=<PASTE REAL VERSION FROM STEP 1>
ARG SUPERCRONIC_SHA256=<PASTE REAL SHA256 FROM STEP 1>
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - && \
    chmod +x /usr/local/bin/supercronic

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /build/.venv /app/.venv
COPY src /app/src
COPY scripts /app/scripts
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" PYTHONPATH="/app/src"

USER appuser
```

- [ ] **Step 5: Write `.dockerignore`**

```
.venv/
.uv-cache/
.tmp/
.pytest_cache/
.pytest-tmp-*/
.mypy_cache/
.ruff_cache/
__pycache__/
node_modules/
tests/
docs/
.git/
*.md
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/security/test_container_image.py -v`
Expected: PASS, all 4 tests. If `ffmpeg`/`supercronic` fail, check the
actual resolved paths/version string from Step 1 before adjusting the
Dockerfile.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile .dockerignore tests/security/test_container_image.py
git commit -m "T18: add pinned, non-root shared container image"
```

---

## Task 4: `docker-compose.yml` — services, networks, hardening flags, digest pinning

**Files:**
- Create: `docker-compose.yml`
- Test: `tests/security/test_container_deployment.py` (new)
- Test: `tests/security/test_pinned_model.py` (new)

**Interfaces:**
- Consumes: the `personal-voice-msg` image from Task 3, `scripts/run_discovery_worker.py` (Task 2).
- Produces: a real running `app`/`discovery` compose stack other tasks in this plan attach fault-injection tests to.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_container_deployment.py
from __future__ import annotations

import json
import subprocess

import pytest

pytestmark = pytest.mark.security

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


@pytest.fixture(scope="module", autouse=True)
def _compose_stack(tmp_path_factory: pytest.TempPathFactory):
    secret_root = tmp_path_factory.mktemp("t18-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    env = {"SECRET_ROOT": str(secret_root)}
    subprocess.run(["docker", "compose", "build"], check=True, capture_output=True)
    subprocess.run(COMPOSE + ["up", "-d"], check=True, capture_output=True, env=_full_env(env))
    yield
    subprocess.run(COMPOSE + ["down", "-v"], capture_output=True)


def _full_env(overrides: dict[str, str]) -> dict[str, str]:
    import os
    merged = dict(os.environ)
    merged.update(overrides)
    return merged


def _inspect(service: str) -> dict:
    result = subprocess.run(
        COMPOSE + ["ps", "-q", service], capture_output=True, text=True, check=True,
    )
    container_id = result.stdout.strip()
    inspected = subprocess.run(
        ["docker", "inspect", container_id], capture_output=True, text=True, check=True,
    )
    return json.loads(inspected.stdout)[0]


@pytest.mark.parametrize("service", ["app", "discovery"])
def test_service_publishes_no_ports(service: str) -> None:
    info = _inspect(service)
    assert info["NetworkSettings"]["Ports"] in ({}, None) or all(
        v is None for v in info["NetworkSettings"]["Ports"].values()
    )


@pytest.mark.parametrize("service", ["app", "discovery"])
def test_service_runs_non_root_with_dropped_capabilities(service: str) -> None:
    info = _inspect(service)
    host_config = info["HostConfig"]
    assert host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["PidsLimit"] not in (0, None, -1)


@pytest.mark.parametrize("service", ["app", "discovery"])
def test_service_has_no_docker_socket_mount(service: str) -> None:
    info = _inspect(service)
    mounts = [m["Source"] for m in info["Mounts"]]
    assert not any("docker.sock" in source for source in mounts)


def test_discovery_network_has_ipv6_disabled() -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", "personal_voice_msg_discovery_net"],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(result.stdout)[0]
    assert info["EnableIPv6"] is False


def test_discovery_service_has_no_secret_bind_mount() -> None:
    info = _inspect("discovery")
    mounts = [m.get("Source", "") for m in info["Mounts"]]
    assert not any("secret" in source.lower() for source in mounts)
```

```python
# tests/security/test_pinned_model.py
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

GEMINI_MODEL = "gemini-3.6-flash"


def test_generation_module_still_pins_the_qualified_gemini_model() -> None:
    """AGENTS.md documents gemini-3.6-flash as the T10/T11-qualified pin.
    Fails if a future edit changes the model string without a matching
    requalification -- catch drift at CI time, not in production."""
    generation_source = Path("src/personal_voice_msg/generation").rglob("*.py")
    found = any(
        GEMINI_MODEL in path.read_text(encoding="utf-8") for path in generation_source
    )
    assert found, (
        f"expected the pinned model {GEMINI_MODEL!r} somewhere under "
        "src/personal_voice_msg/generation/ -- if this changed intentionally, "
        "it requires full T10/T11 requalification per AGENTS.md, not a quiet edit"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/security/test_container_deployment.py tests/security/test_pinned_model.py -v`
Expected: FAIL — no `docker-compose.yml` exists yet;
`test_pinned_model.py` may already pass (check first — if the model string
already appears, it's a genuine pass now, not a red-test failure; keep the
test regardless as regression coverage this task adds).

- [ ] **Step 3: Resolve the built image's digest and write `docker-compose.yml`**

```bash
docker build -t personal-voice-msg:t18 .
docker inspect personal-voice-msg:t18 --format '{{.Id}}'
```

```yaml
services:
  app:
    image: personal-voice-msg:t18
    build: .
    user: "10001"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: ["ALL"]
    security_opt: ["no-new-privileges:true"]
    pids_limit: 512
    mem_limit: 8g
    cpus: 4
    networks: [app_net]
    volumes:
      - db_data:/data
      - type: bind
        source: ${SECRET_ROOT}
        target: /secrets
        read_only: true
    # Placeholder until Task 7 adds scripts/crontab -- Task 7 Step 3
    # replaces this with ["supercronic", "/app/scripts/crontab"], the only
    # place this line changes after this task.
    command: ["sleep", "300"]
    restart: unless-stopped

  discovery:
    image: personal-voice-msg:t18
    build: .
    user: "10001"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: ["ALL"]
    security_opt: ["no-new-privileges:true"]
    pids_limit: 64
    mem_limit: 1g
    cpus: 1
    networks: [discovery_net]
    # No `searxng` service exists in this file, and none is added by T18:
    # like the weekly-discovery-pipeline gap noted above, "what SearXNG
    # instance does production discovery search against" is a pre-existing
    # open question this task does not solve (no self-hosted SearXNG has
    # ever been deployed anywhere in this project's history; T07's own
    # tests reach a real SearXNG only via a throwaway Docker test fixture,
    # T07_NETWORK_HARNESS/SEARXNG_URL). The discovery container's job here
    # is to exist as a correctly networked, correctly resource-bounded,
    # correctly isolated container for T18's own hardening tests -- not to
    # produce real discovery output. Once a real SearXNG target is decided,
    # the owner runs the real verification harness manually:
    # `docker compose exec discovery python scripts/run_discovery_worker.py
    # --searxng-base-url <real-url> --budget-seconds 60` (documented in
    # infra/RUNBOOK.md, Task 9).
    command: ["sleep", "300"]
    restart: "no"

networks:
  app_net:
    enable_ipv6: false
  discovery_net:
    enable_ipv6: false

volumes:
  db_data:
```

The YAML above already shows `app`'s final intended command
(`["supercronic", "/app/scripts/crontab"]`), but `/app/scripts/crontab`
doesn't exist yet — Task 7 adds it. To keep this task's own tests
independent of Task 7, write `app`'s command in the actual
`docker-compose.yml` file as `["sleep", "300"]` for now (same placeholder
pattern as `discovery`'s command above, for the same reason: something has
to keep the container alive for `docker inspect`/fault-injection tests
without depending on a later task). Task 7 Step 3 replaces this placeholder
with the real supercronic command — that is the *only* place `app`'s
command changes after this task.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/security/test_container_deployment.py tests/security/test_pinned_model.py -v`
Expected: PASS, all tests.

- [ ] **Step 5: Pin the image digest and requalification comment**

Edit `docker-compose.yml`: change both `image:` lines to
`personal-voice-msg:t18@sha256:<the Id from Step 3, stripped of its
"sha256:" prefix duplication>` and add a comment above `services:` —
`# Image digest pinned <date>. Rebuild + rerun tests/security/
test_container_image.py and this file's suite before changing.` Re-run
Step 4's test to confirm the pinned reference still resolves.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml tests/security/test_container_deployment.py tests/security/test_pinned_model.py
git commit -m "T18: add hardened two-service docker-compose.yml"
```

---

## Task 5: Network egress isolation fault injection

**Files:**
- Test: `tests/security/test_network_isolation.py` (new)

**Interfaces:**
- Consumes: the running `app`/`discovery` compose stack from Task 4.
- Produces: no new production code — this task is pure verification, proving the compose topology actually enforces the isolation Task 4 configured.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_network_isolation.py
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.security

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


def _exec(service: str, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        COMPOSE + ["exec", "-T", service, *cmd],
        capture_output=True, text=True, timeout=15,
    )


def test_discovery_cannot_reach_the_app_container_at_all() -> None:
    result = _exec("discovery", "python3", "-c",
        "import socket; socket.create_connection(('app', 80), timeout=3)")
    assert result.returncode != 0


def test_discovery_cannot_reach_a_private_network_address() -> None:
    result = _exec("discovery", "python3", "-c",
        "import socket; socket.create_connection(('192.168.1.1', 80), timeout=3)")
    assert result.returncode != 0


def test_app_cannot_reach_the_discovery_container() -> None:
    result = _exec("app", "python3", "-c",
        "import socket; socket.create_connection(('discovery', 80), timeout=3)")
    assert result.returncode != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.yml up -d && uv run pytest tests/security/test_network_isolation.py -v`
Expected: on default Compose networking, `app`/`discovery` are on
*different* Docker networks with no shared network, so these should
already pass structurally — run first to confirm; if any fails
unexpectedly, that is the real red-test signal telling you Task 4's
network separation is incomplete (e.g. a stray default network Compose
adds both services to). Fix `docker-compose.yml`, not this test.

- [ ] **Step 3: If Step 2 surfaced a gap, fix `docker-compose.yml`**

The most common cause: Compose puts every service without an explicit
`networks:` key on one default network. Since Task 4 already gave both
services explicit `networks:` lists with no shared entry, this should not
recur — if it does, check for a stray default network via
`docker network ls | grep personal_voice_msg` and confirm neither
container is attached to it (`docker inspect <container> --format
'{{json .NetworkSettings.Networks}}'`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/security/test_network_isolation.py -v`
Expected: PASS, all 3 tests.

- [ ] **Step 5: Commit**

```bash
git add tests/security/test_network_isolation.py
git commit -m "T18: verify discovery/app network isolation with real fault injection"
```

---

## Task 6: Resource exhaustion terminates the bounded discovery worker

**Files:**
- Test: `tests/security/test_discovery_resource_limits.py` (new)

**Interfaces:**
- Consumes: `personal-voice-msg:t18` image (Task 3), `scripts/run_discovery_worker.py` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_discovery_resource_limits.py
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.security

IMAGE = "personal-voice-msg:t18"


def test_pids_limit_blocks_fork_bombing_the_discovery_worker() -> None:
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--pids-limit", "10",
            IMAGE, "sh", "-c",
            ": & : & : & : & : & : & : & : & : & : & : & : & : & : & : &"
            "python3 -c \"import os\nfor _ in range(200):\n os.fork()\"",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0


def test_memory_limit_kills_a_deliberately_oversized_allocation() -> None:
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--memory", "64m",
            IMAGE, "python3", "-c", "x = bytearray(500 * 1024 * 1024)",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/security/test_discovery_resource_limits.py -v`
Expected: FAIL only if `personal-voice-msg:t18` doesn't exist yet (build it
first per Task 3/4) — otherwise these should pass immediately since they
test Docker's own real cgroup enforcement, not new application code. If
either test unexpectedly passes with `returncode == 0`, that is the real
finding: Docker's cgroup limits aren't being honored on this host (check
`docker info | grep -i cgroup`) — do not weaken the test to work around it.

- [ ] **Step 3: No implementation needed beyond ensuring the image exists**

This task adds no production code — `--pids-limit`/`--memory` are Docker's
own enforcement, already configured for the compose services in Task 4
(`pids_limit`/`mem_limit`). This test proves those exact settings actually
terminate a real runaway process, closing the plan's "resource exhaustion
terminates the bounded discovery worker" red test with real evidence.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/security/test_discovery_resource_limits.py -v`
Expected: PASS, both tests.

- [ ] **Step 5: Commit**

```bash
git add tests/security/test_discovery_resource_limits.py
git commit -m "T18: prove real resource limits terminate the discovery worker"
```

---

## Task 7: Cron timer for the daily-send entrypoint + reboot/restart state test

**Files:**
- Create: `scripts/crontab`
- Modify: `docker-compose.yml` (replace `app`'s temporary `sleep 300` override with the real supercronic command from Task 4 Step 3)
- Test: `tests/integration/test_cron_daily_send_trigger.py` (new)
- Test: `tests/integration/test_restart_state_preservation.py` (new)

**Interfaces:**
- Consumes: `scripts/run_daily_entrypoint.py` (T17b, existing, unchanged).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_cron_daily_send_trigger.py
from __future__ import annotations

import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


def test_supercronic_is_the_apps_running_process() -> None:
    subprocess.run(COMPOSE + ["up", "-d", "app"], check=True, capture_output=True)
    try:
        time.sleep(3)
        result = subprocess.run(
            COMPOSE + ["exec", "-T", "app", "sh", "-c", "ps -o comm= -p 1"],
            capture_output=True, text=True, timeout=15,
        )
        assert "supercronic" in result.stdout
    finally:
        subprocess.run(COMPOSE + ["down"], capture_output=True)
```

```python
# tests/integration/test_restart_state_preservation.py
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.integration

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


def test_restart_preserves_the_data_volume_and_drops_tmpfs_writes() -> None:
    subprocess.run(COMPOSE + ["up", "-d", "app"], check=True, capture_output=True)
    try:
        subprocess.run(
            COMPOSE + ["exec", "-T", "app", "sh", "-c",
                       "echo persisted > /data/marker.txt; echo transient > /tmp/marker.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(COMPOSE + ["restart", "app"], check=True, capture_output=True)
        persisted = subprocess.run(
            COMPOSE + ["exec", "-T", "app", "cat", "/data/marker.txt"],
            capture_output=True, text=True,
        )
        transient = subprocess.run(
            COMPOSE + ["exec", "-T", "app", "cat", "/tmp/marker.txt"],
            capture_output=True, text=True,
        )
        assert persisted.stdout.strip() == "persisted"
        assert transient.returncode != 0
    finally:
        subprocess.run(COMPOSE + ["down", "-v"], capture_output=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_cron_daily_send_trigger.py tests/integration/test_restart_state_preservation.py -v`
Expected: `test_supercronic_is_the_apps_running_process` FAILs (app currently
runs the Task 4 `sleep 300` placeholder). The restart test may already
pass (tmpfs/named-volume behavior is Docker's own, not this task's) — if
so, keep it as regression coverage, it still documents the real property.

- [ ] **Step 3: Add the crontab and wire it into `docker-compose.yml`**

```
# scripts/crontab
# supercronic format: standard 5-field cron + command.
# Matches T17b's documented "every 1-2 minutes" external-timer target.
* * * * * python3 /app/scripts/run_daily_entrypoint.py --config /secrets/app.toml --database /data/app.db
```

In `docker-compose.yml`, remove the temporary `sleep 300` override on
`app` (restore `command: ["supercronic", "/app/scripts/crontab"]` from
Task 4's original draft) — also add a bind mount for the crontab file
itself if it isn't already inside the built image (`scripts/` is already
`COPY`'d into the image in Task 3's Dockerfile, so no new mount is needed
— `/app/scripts/crontab` will already be present).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_cron_daily_send_trigger.py tests/integration/test_restart_state_preservation.py -v`
Expected: PASS, both files.

- [ ] **Step 5: Run the full Task 4 suite again**

Run: `uv run pytest tests/security/test_container_deployment.py -v`
Expected: still PASS — confirms removing the `sleep 300` override didn't
break Task 4's non-root/cap-drop/no-port assertions.

- [ ] **Step 6: Commit**

```bash
git add scripts/crontab docker-compose.yml tests/integration/test_cron_daily_send_trigger.py tests/integration/test_restart_state_preservation.py
git commit -m "T18: wire supercronic daily-send timer into the app container"
```

---

## Task 8: Git-history and built-image secret scanning

**Files:**
- Modify: `scripts/repository_policy.py`
- Test: `tests/security/test_repository_policy_history_and_image.py` (new)

**Interfaces:**
- Consumes: `scripts/repository_policy.py`'s existing `GITHUB_TOKEN`,
  `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `PRIVATE_KEY`,
  `SENSITIVE_ARTIFACT_NAMES`, `SENSITIVE_ARTIFACT_SUFFIXES`,
  `DOCUMENTATION_SUFFIXES` (all existing module-level constants), `CHECKS`
  dict and `parse_args`/`main` (existing CLI wiring — read these before
  writing Step 3 so the new checks register the same way `check_secrets`
  does).
- Produces: `check_git_history(root: Path) -> list[str]`,
  `check_image_secrets(image: str) -> list[str]`, both added to the
  existing `CHECKS` registry as `"git-history"` and `"image"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_repository_policy_history_and_image.py
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from repository_policy import check_git_history, check_image_secrets  # noqa: E402


def _init_repo_with_a_deleted_secret(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    secret_file = root / "oops.txt"
    secret_file.write_text("token: ghp_" + "a" * 36, encoding="utf-8")
    subprocess.run(["git", "add", "oops.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add secret"], cwd=root, check=True)
    secret_file.write_text("cleaned", encoding="utf-8")
    subprocess.run(["git", "add", "oops.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove secret"], cwd=root, check=True)


def test_check_git_history_catches_a_secret_deleted_in_a_later_commit(tmp_path: Path) -> None:
    _init_repo_with_a_deleted_secret(tmp_path)
    violations = check_git_history(tmp_path)
    assert any("credential" in v for v in violations), violations


def test_check_git_history_is_clean_on_a_repo_with_no_secrets(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "readme.txt").write_text("nothing sensitive here", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    assert check_git_history(tmp_path) == []


def test_check_image_secrets_catches_a_secret_baked_into_a_layer(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM busybox\nRUN echo 'AIza" + "b" * 35 + "' > /baked-secret.txt\n",
        encoding="utf-8",
    )
    image_tag = "t18-repo-policy-test-image"
    subprocess.run(
        ["docker", "build", "-t", image_tag, str(tmp_path)], check=True, capture_output=True,
    )
    try:
        violations = check_image_secrets(image_tag)
        assert any("credential" in v for v in violations), violations
    finally:
        subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/security/test_repository_policy_history_and_image.py -v`
Expected: FAIL with `ImportError: cannot import name 'check_git_history'`.

- [ ] **Step 3: Implement in `scripts/repository_policy.py`**

Read the file's existing `check_secrets`/`CHECKS`/`parse_args` first (they
already exist — do not redefine `GITHUB_TOKEN` etc., reuse the module-level
constants). Add:

```python
def _scan_content_for_secrets(content: str, label: str) -> list[str]:
    violations: list[str] = []
    if GITHUB_TOKEN.search(content):
        violations.append(f"credential detected: {label}")
    if GEMINI_API_KEY.search(content):
        violations.append(f"credential detected: {label}")
    if TELEGRAM_BOT_TOKEN.search(content):
        violations.append(f"credential detected: {label}")
    if PRIVATE_KEY.search(content):
        violations.append(f"private key detected: {label}")
    return violations


def check_git_history(root: Path) -> list[str]:
    violations: list[str] = []
    listing = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--all", "--objects"],
        capture_output=True, text=True, check=True,
    )
    for line in listing.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        blob_sha, path_in_history = parts
        filename = Path(path_in_history).name.casefold()
        documented_example = filename.endswith(".example") or any(
            filename.endswith(f".example{suffix}") for suffix in DOCUMENTATION_SUFFIXES
        )
        sensitive_filename = (
            filename in SENSITIVE_ARTIFACT_NAMES
            or Path(path_in_history).suffix.casefold() in SENSITIVE_ARTIFACT_SUFFIXES
        )
        if sensitive_filename and not documented_example:
            violations.append(
                f"sensitive artifact detected in history: {path_in_history}@{blob_sha[:12]}"
            )
            continue
        content = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-p", blob_sha],
            capture_output=True, text=True, errors="ignore", check=False,
        ).stdout
        violations.extend(
            v.replace(path_in_history, f"{path_in_history}@{blob_sha[:12]}")
            for v in _scan_content_for_secrets(content, path_in_history)
        )
    return violations


def check_image_secrets(image: str) -> list[str]:
    violations: list[str] = []
    container = subprocess.run(
        ["docker", "create", image], capture_output=True, text=True, check=True,
    ).stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as extract_dir:
            export = subprocess.run(
                ["docker", "export", container], capture_output=True, check=True,
            )
            with tarfile.open(fileobj=io.BytesIO(export.stdout)) as archive:
                archive.extractall(extract_dir, filter="data")
            for path in Path(extract_dir).rglob("*"):
                if not path.is_file():
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                label = str(path.relative_to(extract_dir))
                violations.extend(_scan_content_for_secrets(content, label))
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    return violations
```

Add `import io`, `import tarfile`, `import tempfile` to the top imports.
Register both in the existing `CHECKS` dict (find its current definition —
likely near `parse_args`) as `"git-history": check_git_history` and add an
`--image` argparse option so `"image"` can run standalone
(`check_image_secrets` needs an image tag, not a root path — thread this
through `main()`'s existing dispatch, matching how `check` already
dispatches by name).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/security/test_repository_policy_history_and_image.py -v`
Expected: PASS, all 3 tests.

- [ ] **Step 5: Run the full repository policy suite and existing regression**

Run: `uv run python scripts/repository_policy.py all --root .`
Expected: clean, exit 0 — confirms the new checks don't break the existing
`all` dispatch or false-positive against this repo's own real history (a
real risk: if this repo's history genuinely contains something matching
these patterns, this is the first time it would be caught — investigate
any real hit before assuming it's a false positive).

- [ ] **Step 6: Commit**

```bash
git add scripts/repository_policy.py tests/security/test_repository_policy_history_and_image.py
git commit -m "T18: scan full git history and built images for leaked secrets"
```

---

## Task 9: Firewall / WireGuard / SSH hardening infra-as-code + local verification + runbook

**Files:**
- Create: `infra/firewall/rules.nft`
- Create: `infra/wireguard/wg0.conf.template`
- Create: `infra/ssh/sshd_config.d/10-hardening.conf`
- Create: `infra/RUNBOOK.md`
- Test: `tests/security/test_firewall_rules.py` (new)

**Interfaces:** none — infra-as-code plus a Docker-based verification test, no Python production interfaces.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_firewall_rules.py
from __future__ import annotations

import subprocess
import time
import uuid

import pytest

pytestmark = pytest.mark.security


def test_only_wireguard_udp_port_is_reachable_under_the_real_ruleset() -> None:
    container = f"t18-nft-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", container,
                "--cap-add", "NET_ADMIN",
                "-v", f"{__file__.rsplit('tests', 1)[0]}infra/firewall/rules.nft:/rules.nft:ro",
                "-p", "51820:51820/udp",
                "-p", "9999:9999/tcp",
                "nixery.dev/nftables/iproute2/python3",
                "sh", "-c",
                "python3 -m http.server 9999 & nft -f /rules.nft && sleep 30",
            ],
            check=True, capture_output=True, text=True,
        )
        time.sleep(3)
        blocked = subprocess.run(
            ["curl", "-sf", "--max-time", "2", "http://127.0.0.1:9999/"],
            capture_output=True,
        )
        assert blocked.returncode != 0
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
```

If `nixery.dev/nftables/iproute2/python3` isn't pullable in this
environment, substitute a `debian:bookworm-slim` base with
`apt-get install -y nftables python3` baked into a tiny throwaway
Dockerfile built once at test-session start instead — the point is a real
container that can actually load an nftables ruleset (needs `NET_ADMIN`),
not the specific base image.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/security/test_firewall_rules.py -v`
Expected: FAIL — `infra/firewall/rules.nft` doesn't exist.

- [ ] **Step 3: Write the infra-as-code**

```nft
# infra/firewall/rules.nft
# Applied on the real VPS per infra/RUNBOOK.md. Verified locally (not
# against a live VPS -- none exists yet) by tests/security/
# test_firewall_rules.py against a real throwaway container.
table inet filter {
    chain input {
        type filter hook input priority 0; policy drop;
        ct state established,related accept
        iif "lo" accept
        udp dport 51820 accept comment "WireGuard administration endpoint"
        ip protocol icmp accept
    }
    chain forward {
        type filter hook forward priority 0; policy drop;
    }
    chain output {
        type filter hook output priority 0; policy accept;
    }
}
```

```ini
# infra/wireguard/wg0.conf.template
# Real keys are generated on the VPS itself (`wg genkey | tee privatekey
# | wg pubkey > publickey`) -- never commit real keys. Copy this file to
# /etc/wireguard/wg0.conf on the VPS and fill in the placeholders.
[Interface]
Address = 10.66.66.1/24
ListenPort = 51820
PrivateKey = <REPLACE_WITH_REAL_SERVER_PRIVATE_KEY_GENERATED_ON_THE_VPS>

[Peer]
# One [Peer] block per administrator device.
PublicKey = <REPLACE_WITH_REAL_PEER_PUBLIC_KEY>
AllowedIPs = 10.66.66.2/32
```

```ini
# infra/ssh/sshd_config.d/10-hardening.conf
PasswordAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
```

```markdown
# infra/RUNBOOK.md
# Deploying to a real VPS

Run once, on a fresh VPS, by the owner (payment/provisioning and live
external verification are outside what an agent session can do -- see
docs/superpowers/specs/2026-08-24-t18-cloud-container-hardening-design.md).

1. `apt-get install wireguard nftables`
2. Copy `infra/ssh/sshd_config.d/10-hardening.conf` to
   `/etc/ssh/sshd_config.d/` on the VPS, then `systemctl restart sshd`
   -- **from a session already using key-based auth**, to avoid locking
   yourself out.
3. `wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey`
   on the VPS; on your own admin device, generate a client keypair the
   same way. Fill both into a real copy of
   `infra/wireguard/wg0.conf.template` at `/etc/wireguard/wg0.conf`.
   `systemctl enable --now wg-quick@wg0`.
4. `nft -f infra/firewall/rules.nft` on the VPS, then
   `nft list ruleset > /etc/nftables.conf` and enable the `nftables`
   systemd unit so it survives reboot.
5. From a **different** machine (not the VPS, not over the VPN), run
   `nmap -Pn <public-ip>` and confirm only `51820/udp` (or nothing, since
   nmap's default scan is TCP) is reported open. This is the literal
   "external port scan finds no application ports" verification the plan
   requires -- it can only be run against a real public IP, which is why
   it's here and not in this repo's test suite.
6. `docker compose up -d` on the VPS, using a real `SECRET_ROOT` outside
   the checked-out repository per `config.py`'s existing enforcement. The
   `discovery` service's default command is a bounded placeholder
   (`sleep 300`) -- no real SearXNG deployment exists yet (a pre-existing
   gap, not something this task solves; see `docker-compose.yml`'s comment
   on the `discovery` service). Once a real SearXNG target is decided, run
   the actual verification harness manually: `docker compose exec
   discovery python scripts/run_discovery_worker.py --searxng-base-url
   <real-url> --budget-seconds 60`.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/security/test_firewall_rules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/ tests/security/test_firewall_rules.py
git commit -m "T18: add firewall/WireGuard/SSH hardening infra-as-code and deployment runbook"
```

---

## Task 10: Task log, live-verification fold-in, independent review, PR

**Files:**
- Create: `docs/task-logs/T18.md`
- Modify: `AGENTS.md` (§Current status and blockers, §Confirmed stack if `docker-compose.yml` is mentioned as absent anywhere)
- Modify: `IMPLEMENTATION_PLAN.md` (T18's `Done when` — mark closed once the below is true)

- [ ] **Step 1: Run the full regression suite**

```bash
uv sync --locked
uv run pytest -m fast
uv run pytest -m integration
uv run pytest -m security
uv run ruff check .
uv run mypy src
uv run python scripts/repository_policy.py all --root .
docker compose config --quiet
```

Record every command's real output in `docs/task-logs/T18.md` — do not
summarize as "passed," paste the actual pass/fail counts, matching every
prior task log's convention (see `docs/task-logs/T17b.md` for the format).

- [ ] **Step 2: Document the two folded-in live-verification items**

In `docs/task-logs/T18.md`, write out the exact commands for:
1. `tests/integration/test_consent_integration.py::test_a_real_exact_stop_from_the_enrolled_chat_disables_sending_durably`
2. Confirming the Task 7 cron timer actually fired during a real
   07:00-07:05 Pacific window (check the app container's logs / the
   resulting `MessageState` in the real database after a real day).

State plainly that this session does not run them (same reason T17b
couldn't: no genuine TLS path to `api.telegram.org` from this sandbox) and
that the owner needs to run them and report results, which then get
appended to this same task log — do not claim them done.

- [ ] **Step 3: Update `AGENTS.md` and `IMPLEMENTATION_PLAN.md`**

Add a dated entry to `AGENTS.md`'s "Current status and blockers" /
"Immediate next step" describing T18 as complete pending the two live
items, pointing at `docs/task-logs/T18.md`, following the exact style of
the T16b/T17/T17b entries already there. Update `IMPLEMENTATION_PLAN.md`'s
T18 section's "Done when" line to reflect what's actually been verified.

- [ ] **Step 4: Request independent security review**

T18 is on `AGENTS.md`'s fixed mandatory-review list. Dispatch a fresh,
unbiased review (new subagent or session, no prior bias toward this
branch's own code) per this project's established T15/T16/T16b/T17/T17b
precedent — tracing the actual compose/firewall/secret-permission logic
against source, not this task log's prose. Fix any Important finding and
re-review before merge.

- [ ] **Step 5: Commit, push, open PR, merge**

```bash
git add docs/task-logs/T18.md AGENTS.md IMPLEMENTATION_PLAN.md
git commit -m "T18: record verification evidence and update task status"
git push -u origin task/T18-cloud-container-hardening
gh pr create --title "T18: cloud and container hardening" --body "..."
gh pr merge --merge --delete-branch
```

---

## Self-review notes (from the plan author, not a step to execute)

- **Spec coverage:** every numbered component in the design spec (1-9) maps
  to a task above (1→Task1, 2→Task2, 3→Task3, 4→Task4, 5→Task7, 6→Task4/
  Task7 restart test, 7→Task8, 8→Task9, "live verification"→Task10).
- **`FetchPolicy()`'s real constructor** (Task 2) must be checked against
  actual source before writing final code — flagged explicitly in that
  task rather than guessed.
- **Digest/version pins** (Task 3's base image, supercronic; Task 4's
  final image digest) are resolved by running real commands as part of the
  task, not fabricated ahead of time — every step says so explicitly.
- Task 4's temporary `sleep 300` override for `app` (needed because Task 7
  hasn't added the crontab yet) is explicitly called out and explicitly
  cleaned up in Task 7 Step 3 — not left dangling.
- **Pre-flight fix (caught before dispatch, not by a task reviewer):**
  Task 4's original draft had `discovery`'s command invoking
  `run_discovery_worker.py --searxng-base-url http://searxng:8080` against
  a `searxng` compose service that was never defined anywhere — load-bearing
  for Task 5 (whose `docker compose exec discovery ...` calls need a
  container still running, not one that already exited after a fast real
  DNS failure). Fixed: `discovery`'s command is a permanent `sleep 300`
  placeholder (documented inline and in the runbook), not a temporary one —
  there is no future task in this plan that gives it a real SearXNG target,
  same as the weekly-discovery-pipeline gap. Task 2's own test was also
  fixed from a hardcoded `skipif(True, ...)` (a test that can never run) to
  reuse T07's existing `T07_NETWORK_HARNESS`/`SEARXNG_URL` real-network
  harness convention.

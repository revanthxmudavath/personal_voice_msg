from __future__ import annotations

import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


def _full_env(overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(overrides)
    return merged


def test_supercronic_is_the_apps_running_process(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # `app` bind-mounts ${SECRET_ROOT} read-only at /secrets (Task 4).
    # Leaving SECRET_ROOT unset does not fail `up -d` -- Compose defaults
    # the unset variable to a blank string, and Docker resolves a blank
    # bind-mount source to the *current working directory*, silently
    # mounting this whole repo checkout read-only at /secrets instead of
    # erroring (confirmed empirically). A real secret root, matching Task
    # 4's own verified `_compose_stack` fixture pattern, avoids that.
    secret_root = tmp_path_factory.mktemp("t18-cron-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    env = _full_env({"SECRET_ROOT": str(secret_root)})

    subprocess.run(
        COMPOSE + ["up", "-d", "app"], check=True, capture_output=True, env=env,
    )
    try:
        time.sleep(3)
        # `ps` is not installed in the python:3.12-slim runtime image (no
        # procps package -- confirmed empirically: `sh -c "ps -o comm= -p
        # 1"` fails with "ps: not found", exit 127). /proc/1/comm is the
        # kernel-provided, dependency-free way to read PID 1's command
        # name and needs no extra package.
        result = subprocess.run(
            COMPOSE + ["exec", "-T", "app", "cat", "/proc/1/comm"],
            capture_output=True, text=True, timeout=15,
        )
        assert "supercronic" in result.stdout
    finally:
        subprocess.run(COMPOSE + ["down"], capture_output=True)

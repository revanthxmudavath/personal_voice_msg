from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = pytest.mark.integration

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]


def _full_env(overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(overrides)
    return merged


def test_restart_preserves_the_data_volume_and_drops_tmpfs_writes(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # See test_cron_daily_send_trigger.py for why SECRET_ROOT must be set
    # explicitly: left unset, Compose/Docker silently bind-mount this repo
    # checkout at /secrets instead of failing.
    secret_root = tmp_path_factory.mktemp("t18-restart-secrets")
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
        # Write as root (--user root), not the container's default appuser
        # (10001, Task 4's `user: "10001"`). Confirmed empirically: a fresh
        # `db_data` named volume is created by Docker owned root:root mode
        # 755 -- nothing in Task 3's Dockerfile or this file pre-creates
        # `/data` with appuser ownership, so appuser cannot write into it
        # at all ("echo ... > /data/marker.txt" as appuser fails with
        # "Permission denied", exit 1). Because the brief's illustrative
        # write used "cmd1; cmd2" (semicolon, not "&&"), that failure was
        # invisible: sh's exit status is cmd2's ("echo ... > /tmp/marker.txt",
        # which appuser *can* write, tmpfs being mode 1777), so
        # check=True never raised and the real failure only surfaced
        # later as an empty /data/marker.txt. This exercises this task's
        # actual target property -- does Docker's own volume-vs-tmpfs
        # mechanism preserve /data and drop /tmp across a restart --
        # without depending on the separate, pre-existing
        # appuser/`/data`-ownership gap (inherited from Task 4's
        # docker-compose.yml + Task 3's Dockerfile, neither touchable
        # from this task's file scope). That gap is real and also blocks
        # the actual cron job this task wires up from ever writing
        # /data/app.db as appuser in production -- flagged in this
        # task's report, not silently fixed here.
        write_markers = (
            "echo persisted > /data/marker.txt; "
            "echo transient > /tmp/marker.txt"
        )
        subprocess.run(
            COMPOSE
            + ["exec", "-T", "--user", "root", "app", "sh", "-c", write_markers],
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

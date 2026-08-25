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


def _secret_root_env(
    tmp_path_factory: pytest.TempPathFactory, name: str
) -> dict[str, str]:
    # See test_cron_daily_send_trigger.py for why SECRET_ROOT must be set
    # explicitly: left unset, Compose/Docker silently bind-mount this repo
    # checkout at /secrets instead of failing.
    secret_root = tmp_path_factory.mktemp(name)
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    return _full_env({"SECRET_ROOT": str(secret_root)})


def test_appuser_can_write_to_the_data_volume(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # Dedicated regression coverage for the "fresh db_data volume is
    # root:root 755, appuser can't write" bug: a fresh named volume that
    # Docker creates is owned by whatever the image owns at that path (or
    # root:root 755 if the image has nothing there). The Dockerfile now
    # pre-creates and chowns /data to appuser precisely so this write
    # succeeds -- the same write the real cron job depends on every
    # minute. `down -v` in `finally` guarantees this test always runs
    # against a genuinely fresh volume, not a stale one left over from an
    # earlier run (an already-existing volume would not retroactively
    # pick up a Dockerfile ownership change).
    env = _secret_root_env(tmp_path_factory, "t18-data-write-secrets")
    subprocess.run(
        COMPOSE + ["up", "-d", "app"], check=True, capture_output=True, env=env,
    )
    try:
        write = subprocess.run(
            COMPOSE + ["exec", "-T", "app", "sh", "-c",
                       "echo write-ok > /data/write-check.txt"],
            capture_output=True, text=True,
        )
        assert write.returncode == 0, (
            f"appuser could not write to /data (expected the Dockerfile's "
            f"mkdir+chown step to make this succeed): {write.stderr}"
        )
        read_back = subprocess.run(
            COMPOSE + ["exec", "-T", "app", "cat", "/data/write-check.txt"],
            capture_output=True, text=True,
        )
        assert read_back.stdout.strip() == "write-ok"
    finally:
        subprocess.run(COMPOSE + ["down", "-v"], capture_output=True)


def test_restart_preserves_the_data_volume_and_drops_tmpfs_writes(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    env = _secret_root_env(tmp_path_factory, "t18-restart-secrets")

    subprocess.run(
        COMPOSE + ["up", "-d", "app"], check=True, capture_output=True, env=env,
    )
    try:
        # Write as the container's default exec user -- appuser (10001,
        # Task 4's `user: "10001"`), the same user the real cron job runs
        # as. This proves the real production path: a fresh `db_data`
        # named volume is created by Docker owned root:root mode 755 by
        # default, so appuser could not write here until the Dockerfile
        # pre-created and chowned /data to appuser (see the Dockerfile
        # comment next to `RUN mkdir -p /data && chown appuser:appuser
        # /data`). If that chown step ever regresses, this write fails
        # with "Permission denied" and this test fails loudly for the
        # right reason -- it must not be weakened back to `--user root`
        # to paper over that.
        write_markers = (
            "echo persisted > /data/marker.txt; "
            "echo transient > /tmp/marker.txt"
        )
        subprocess.run(
            COMPOSE + ["exec", "-T", "app", "sh", "-c", write_markers],
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

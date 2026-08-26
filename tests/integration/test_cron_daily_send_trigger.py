"""Real proof that supercronic actually *fires* the production crontab entry
inside the running `app` container -- not just that supercronic is PID 1.

T18 whole-branch review finding I3: this module previously only asserted
`/proc/1/comm` == supercronic, which is static configuration. It could not
have caught finding C2 (the crontab's `--config` path being unloadable), and
in fact did not.

What this now does, with nothing faked: brings the real `app` service up
with a real secret mount and a real `/conf/app.toml`, waits past a real
wall-clock minute boundary, and reads supercronic's own log to confirm the
literal crontab command ran to completion with exit status 0 and printed the
entrypoint's own "not due, skipped" line. A configuration failure -- C2's
exact symptom -- surfaces there as a non-zero job exit and a
ConfigurationError on the job's stderr channel, so this test fails loudly on
it.

Deviation from the design spec's §5 sketch, stated plainly: the spec wanted a
fake local HTTP server standing in for `api_base` and an assertion that it
received a request. That cannot happen outside a genuine 07:00-07:05 Pacific
window: `run_daily_entrypoint` classifies the DAILY_SEND trigger first and
returns `None` -- touching neither the network nor the database -- at every
other minute of the day (that is deliberate; the entrypoint is designed to be
safe to call on every tick all day). Making the fake server receive anything
would require patching `scheduling.py`'s trigger time, which T17b already
recorded as explicitly not acceptable evidence in this project. The real
07:00 Pacific firing therefore remains an owner-run live-verification item
(docs/task-logs/T18.md); what is proved here for real is the part that is
provable: cron fires the real command every minute, and the real command's
configuration loads and it exits 0.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.docker]

# `-p personal_voice_msg_test`: never share the production Compose project.
COMPOSE = [
    "docker", "compose", "-p", "personal_voice_msg_test",
    "-f", "docker-compose.yml",
]

APP_TOML = """\
profile = "development"
secret_root = "/secrets"
telegram_chat_id_file = "telegram_chat_id.json"
telegram_bot_token_file = "telegram-token.txt"
voice_embedding_file = "voice.embedding"
sender_auth_key_file = "sender-auth-key.txt"
"""


def _full_env(overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(os.environ)
    merged.update(overrides)
    return merged


def _deployment_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    # (see _build_image below for why the image is built explicitly)
    # SECRET_ROOT and APP_CONFIG_DIR are declared with Compose's
    # required-variable syntax in docker-compose.yml, so `up` fails loudly
    # if either is unset rather than silently bind-mounting this repo
    # checkout at /secrets (the pre-I1 behaviour, confirmed empirically).
    secret_root = tmp_path_factory.mktemp("t18-cron-secrets")
    (secret_root / "telegram_chat_id.json").write_text(
        '{"profile": "development", "telegram_chat_id": 1}'
    )
    (secret_root / "telegram-token.txt").write_text("x" * 40)
    (secret_root / "voice.embedding").write_bytes(b"x")
    (secret_root / "sender-auth-key.txt").write_text("x" * 32)
    # `development` profile here, unlike test_container_config_loads.py:
    # these files are bind-mounted from the host filesystem, whose uid/mode
    # this test cannot control portably (this project's own sandbox is
    # Windows). The deployed-profile ownership/mode path and the /conf vs
    # /secrets separation that finding C2 was actually about are covered
    # for real, with a `production` profile and real chown/chmod, by
    # tests/security/test_container_config_loads.py.
    config_dir = tmp_path_factory.mktemp("t18-cron-conf")
    (config_dir / "app.toml").write_text(APP_TOML)
    # Exported into this process's environment too, not just returned:
    # docker-compose.yml declares both with Compose's required-variable
    # syntax and *every* compose subcommand interpolates the whole file,
    # including the bare `docker compose exec` calls below.
    os.environ["SECRET_ROOT"] = str(secret_root)
    os.environ["APP_CONFIG_DIR"] = str(config_dir)
    env = _full_env({
        "SECRET_ROOT": str(secret_root),
        "APP_CONFIG_DIR": str(config_dir),
    })
    _build_image(env)
    return env


def _build_image(env: dict[str, str]) -> None:
    # Built explicitly rather than relying on another module's fixture
    # having run first: pytest collects tests/integration before
    # tests/security, so nothing guarantees the shared `personal-voice-msg:
    # t18` tag exists by the time this module runs. Layers are cached, so
    # this is nearly free when the image is already current.
    built = subprocess.run(
        COMPOSE + ["build"], capture_output=True, text=True,
        timeout=1800, env=env,
    )
    assert built.returncode == 0, built.stderr


def test_supercronic_is_the_apps_running_process(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    env = _deployment_env(tmp_path_factory)
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
            capture_output=True, text=True, timeout=30,
        )
        assert "supercronic" in result.stdout
    finally:
        subprocess.run(COMPOSE + ["down", "-v"], capture_output=True, env=env)


def test_cron_really_fires_the_production_command_and_it_exits_cleanly(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    env = _deployment_env(tmp_path_factory)
    subprocess.run(
        COMPOSE + ["up", "-d", "app"], check=True, capture_output=True, env=env,
    )
    try:
        # The crontab runs `* * * * *`, so one full minute boundary plus
        # slack is enough for at least one real firing. No test-side
        # invocation of the entrypoint happens anywhere in this test.
        # supercronic's exact markers, confirmed against a real run of this
        # image: `msg="job succeeded"` on a clean exit, and
        # `level=error msg="error running command: exit status 1"` on a
        # failing one (there is no "job failed" string).
        deadline = time.monotonic() + 180
        logs = ""
        while time.monotonic() < deadline:
            time.sleep(10)
            logs = subprocess.run(
                COMPOSE + ["logs", "--no-color", "app"],
                capture_output=True, text=True, timeout=60, env=env,
            ).stdout
            if "job succeeded" in logs or "error running command" in logs:
                break

        assert "error running command" not in logs, (
            "supercronic reported a FAILED run of the production crontab "
            "command. This is exactly finding C2's symptom: a "
            "ConfigurationError on the job's stderr channel means --config "
            f"points somewhere unloadable. Full logs:\n{logs}"
        )
        assert "job succeeded" in logs, (
            "supercronic never reported a completed run of the crontab "
            f"command within 180s. Full logs:\n{logs}"
        )
        # The entrypoint's own stdout, forwarded by supercronic on its
        # channel=stdout lines. Asserting on it -- not just on supercronic's
        # exit bookkeeping -- is what proves the *Python program* ran and
        # got far enough to classify today's trigger, i.e. that
        # load_settings() succeeded against the crontab's real --config path.
        assert "not due, skipped" in logs, (
            "expected the cron-fired entrypoint to print its own 'not due, "
            "skipped' line (run_daily_entrypoint returns None outside the "
            "07:00-07:05 Pacific DAILY_SEND window). Its absence means the "
            "process died before reaching that point -- e.g. on "
            f"configuration loading. Full logs:\n{logs}"
        )
        assert "ConfigurationError" not in logs, (
            f"a ConfigurationError reached the cron job's output:\n{logs}"
        )
    finally:
        subprocess.run(COMPOSE + ["down", "-v"], capture_output=True, env=env)

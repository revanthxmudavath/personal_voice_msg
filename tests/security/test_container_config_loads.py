"""Real proof that the *production* cron command's configuration actually
loads inside the built image.

T18 whole-branch review finding C2: `scripts/crontab` ran
`--config /secrets/app.toml`, but `config.py`'s `secret_root()` rejects (for
any non-development profile) a secret root at or below the config file's own
resolved "project root" -- and `_project_root()` falls back to the config
file's own directory when nothing above it contains a pyproject.toml or
.git, which is exactly the situation inside this image. So every real
production tick would have died with `ConfigurationError: deployed secret
root must be outside the project directory`, forever, silently (cron stderr
goes nowhere yet -- T19's scope). Nothing caught it because no test ever ran
the crontab's own argument string against the real image.

Both tests below read that argument string out of `scripts/crontab` itself
rather than hard-coding a path, so the crontab and this test cannot drift
apart again. Everything runs inside the real image against real bind-mounted
volumes; the negative control reproduces the original C2 failure exactly, so
this file demonstrably *would* have caught it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.security, pytest.mark.docker]

REPO_ROOT = Path(__file__).resolve().parents[2]
CRONTAB = REPO_ROOT / "scripts" / "crontab"
IMAGE = "personal-voice-msg:t18"

# A deployed profile on purpose: `development` is exempt from BOTH the
# secret-root-outside-the-project rule and Task 1's secret-file ownership/
# mode checks, so a development-profile test would have passed against the
# broken /secrets/app.toml layout too and proved nothing.
PROFILE = "production"

_LOAD_SETTINGS = (
    "from pathlib import Path\n"
    "from personal_voice_msg.config import load_settings\n"
    "import sys\n"
    "try:\n"
    "    load_settings(Path(sys.argv[1]))\n"
    "    print('LOADED')\n"
    "except Exception as exc:\n"
    "    print(f'{type(exc).__name__}: {exc}')\n"
)


@pytest.fixture(scope="module", autouse=True)
def _built_image() -> None:
    # Build explicitly rather than relying on another module's fixture
    # having run first: pytest collects tests/integration before
    # tests/security and files alphabetically within a directory, so which
    # module happens to build the shared tag is an ordering accident. Layers
    # are cached, so this is nearly free when the image is already current.
    here = Path(__file__).resolve().parent
    built = subprocess.run(
        ["docker", "compose", "-p", "personal_voice_msg_test",
         "-f", "docker-compose.yml", "build"],
        capture_output=True, text=True, timeout=1800,
        env={**os.environ, "SECRET_ROOT": str(here), "APP_CONFIG_DIR": str(here)},
    )
    assert built.returncode == 0, built.stderr


def _crontab_config_argument() -> str:
    """The literal `--config` value the production crontab passes."""

    command = [
        line for line in CRONTAB.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(command) == 1, f"expected exactly one crontab entry, got {command}"
    match = re.search(r"--config\s+(\S+)", command[0])
    assert match, f"no --config argument in the crontab entry: {command[0]!r}"
    return match.group(1)


def _volume(name: str) -> str:
    volume = f"t18-cfg-{name}-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "volume", "create", volume],
        check=True, capture_output=True, text=True,
    )
    return volume


def _seed(volume_mounts: list[str], script: str) -> None:
    """Populate the volumes as root, then hand them to uid 10001.

    Docker volumes are created root:root 0755 and Task 1's permission check
    requires each secret file to be owned by the running identity with no
    group/other bits -- so the seeding step is also the thing that proves
    the documented `chown 10001:10001` / `chmod 600` runbook steps are the
    right ones.
    """

    result = subprocess.run(
        ["docker", "run", "--rm", "--user", "0", *volume_mounts,
         IMAGE, "sh", "-c", script],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"seeding failed: {result.stderr}"


def _secret_seed_commands(directory: str) -> str:
    chat_id = json.dumps({"profile": PROFILE, "telegram_chat_id": 1})
    return (
        f"mkdir -p {directory} && "
        f"printf '%s' '{chat_id}' > {directory}/telegram_chat_id.json && "
        f"printf 'x%.0s' $(seq 40) > {directory}/telegram-token.txt && "
        f"printf 'x' > {directory}/voice.embedding && "
        f"printf 'x%.0s' $(seq 32) > {directory}/sender-auth-key.txt && "
        f"chown -R 10001:10001 {directory} && chmod 600 {directory}/*"
    )


def _app_toml(secret_root: str) -> str:
    return (
        f'profile = "{PROFILE}"\\n'
        f'secret_root = "{secret_root}"\\n'
        'telegram_chat_id_file = "telegram_chat_id.json"\\n'
        'telegram_bot_token_file = "telegram-token.txt"\\n'
        'voice_embedding_file = "voice.embedding"\\n'
        'sender_auth_key_file = "sender-auth-key.txt"\\n'
    )


def _run_load(volume_mounts: list[str], config_path: str) -> str:
    result = subprocess.run(
        ["docker", "run", "--rm", "--user", "10001", *volume_mounts,
         IMAGE, "python3", "-c", _LOAD_SETTINGS, config_path],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the loader helper itself failed to run: {result.stderr}"
    )
    return result.stdout.strip()


def _remove(*volumes: str) -> None:
    for volume in volumes:
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)


def test_the_production_crontabs_own_config_path_loads_inside_the_image() -> None:
    config_path = _crontab_config_argument()
    config_dir = str(Path(config_path).parent.as_posix())
    assert config_dir != "/secrets", (
        "the crontab's config file must not live inside the secret mount -- "
        "that is finding C2 itself (see this module's docstring)"
    )

    conf_volume = _volume("conf")
    secrets_volume = _volume("secrets")
    mounts = [
        "-v", f"{conf_volume}:{config_dir}",
        "-v", f"{secrets_volume}:/secrets",
    ]
    try:
        _seed(mounts, _secret_seed_commands("/secrets"))
        _seed(
            mounts,
            f"mkdir -p {config_dir} && "
            f"printf '{_app_toml('/secrets')}' > {config_path} && "
            f"chown 10001:10001 {config_path}",
        )
        outcome = _run_load(mounts, config_path)
    finally:
        _remove(conf_volume, secrets_volume)

    assert outcome == "LOADED", (
        f"expected `load_settings({config_path})` -- the literal --config "
        f"value scripts/crontab passes on every production tick -- to load "
        f"cleanly inside the real image with /secrets mounted separately. "
        f"Got: {outcome}"
    )


def test_the_pre_fix_layout_still_reproduces_the_c2_failure() -> None:
    """Negative control. Without it, the test above could pass for reasons
    unrelated to the separate `/conf` mount, and would not demonstrate that
    it catches the original bug."""

    secrets_volume = _volume("only-secrets")
    mounts = ["-v", f"{secrets_volume}:/secrets"]
    try:
        _seed(mounts, _secret_seed_commands("/secrets"))
        _seed(
            mounts,
            f"printf '{_app_toml('/secrets')}' > /secrets/app.toml && "
            "chown 10001:10001 /secrets/app.toml && chmod 600 /secrets/app.toml",
        )
        outcome = _run_load(mounts, "/secrets/app.toml")
    finally:
        _remove(secrets_volume)

    assert outcome == (
        "ConfigurationError: deployed secret root must be outside the "
        "project directory"
    ), (
        f"expected the original C2 layout (app.toml inside the secret mount) "
        f"to still fail closed with exactly that ConfigurationError -- if it "
        f"does not, config.py's secret_root() rule changed and the separate "
        f"/conf mount may no longer be necessary. Got: {outcome}"
    )

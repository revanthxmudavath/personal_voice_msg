from __future__ import annotations

import subprocess
import uuid

import pytest

pytestmark = pytest.mark.security

IMAGE = "python:3.12-slim"


def _run_check(
    mode: str, owner_uid: int, check_uid: int
) -> subprocess.CompletedProcess[str]:
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

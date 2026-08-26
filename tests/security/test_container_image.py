from __future__ import annotations

import subprocess

import pytest

pytestmark = [pytest.mark.security, pytest.mark.docker]

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
        ["docker", "run", "--rm", IMAGE, "sh", "-c",
         "command -v docker; ls /var/run/docker.sock"],
        capture_output=True, text=True, timeout=30,
    )
    assert which.returncode != 0
    assert "docker.sock" not in which.stdout


def test_image_has_ffmpeg_and_supercronic() -> None:
    result = subprocess.run(
        ["docker", "run", "--rm", IMAGE, "sh", "-c",
         "ffmpeg -version && supercronic -version"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

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


def test_check_git_history_catches_a_secret_deleted_in_a_later_commit(
    tmp_path: Path,
) -> None:
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


def test_check_image_secrets_catches_a_secret_baked_into_a_layer(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM busybox\nRUN echo 'AIza" + "b" * 35 + "' > /baked-secret.txt\n",
        encoding="utf-8",
    )
    image_tag = "t18-repo-policy-test-image"
    subprocess.run(
        ["docker", "build", "-t", image_tag, str(tmp_path)],
        check=True,
        capture_output=True,
    )
    try:
        violations = check_image_secrets(image_tag)
        assert any("credential" in v for v in violations), violations
    finally:
        subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True)

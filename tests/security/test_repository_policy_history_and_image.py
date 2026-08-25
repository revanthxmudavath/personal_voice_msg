from __future__ import annotations

import io
import subprocess
import sys
import tarfile
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


def test_check_image_secrets_catches_a_sensitive_name_reachable_only_via_a_hard_link(
    tmp_path: Path,
) -> None:
    # Docker image layers routinely contain hard links (e.g. a base image
    # linking one binary name to another already-archived file). A
    # `tarfile` hard-link member (LNKTYPE) has `isfile() == False`, so a
    # naive "only regular files" gate silently skips it -- and with it,
    # any sensitively-named path that exists only as a hard-link alias.
    # This plants exactly that: /id_rsa is a real hard link (`ln`, not
    # `ln -s`) to /data.bin, and /data.bin's own name and content are
    # deliberately unremarkable (no token pattern, not a sensitive name),
    # so the only way to catch /id_rsa is to check a hard-link member's
    # own name, not just regular-file members.
    (tmp_path / "Dockerfile").write_text(
        "FROM busybox\n"
        "RUN echo 'unremarkable content, no token pattern here' > /data.bin"
        " && ln /data.bin /id_rsa\n",
        encoding="utf-8",
    )
    image_tag = "t18-repo-policy-test-hardlink-image"
    subprocess.run(
        ["docker", "build", "-t", image_tag, str(tmp_path)],
        check=True,
        capture_output=True,
    )
    try:
        # Precondition proof: confirm Docker actually materialized
        # /id_rsa as a tar hard-link member (not two independent regular
        # -file copies), so this test genuinely exercises the hard-link
        # code path the fix targets rather than accidentally passing some
        # other way.
        container = subprocess.run(
            ["docker", "create", image_tag],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        try:
            export = subprocess.run(
                ["docker", "export", container], capture_output=True, check=True,
            )
            with tarfile.open(fileobj=io.BytesIO(export.stdout)) as archive:
                id_rsa_member = archive.getmember("id_rsa")
        finally:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        assert id_rsa_member.islnk(), id_rsa_member.type
        assert not id_rsa_member.isfile()

        violations = check_image_secrets(image_tag)
        assert any("id_rsa" in v for v in violations), violations
    finally:
        subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True)

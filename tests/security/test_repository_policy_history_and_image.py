from __future__ import annotations

import io
import os
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


def test_check_image_secrets_skips_elf_content_but_not_plain_text(
    tmp_path: Path,
) -> None:
    # Run against this project's real 5.6 GB image, the content scan
    # reported "private key detected" for four Debian-provided crypto
    # libraries (libgio, libgnutls, libmbedcrypto, libssh). Those hits are
    # genuine PEM text in .rodata -- libssh stores the header strings it
    # parses, libgnutls embeds whole PEM test vectors -- so tightening the
    # regex cannot separate them from a real key. `check_image_secrets`
    # therefore skips CONTENT scanning for members whose first bytes are
    # the ELF magic, and only for those.
    #
    # This plants both halves in one real image and asserts the split: a
    # file that starts with the ELF magic and then carries a PEM header is
    # ignored, while the identical PEM header in a plain-text file is still
    # caught. The rule under test is literally "starts with \x7fELF", so
    # writing those four bytes exercises the real rule -- no compiler
    # needed and nothing stubbed.
    # Split so this source file does not itself trip `check_secrets`, the
    # same trick the `"AIza" + "b" * 35` fixture above uses. The value is
    # the real, contiguous PEM header at runtime.
    header = "-----BEGIN RSA PRIVATE" + " KEY-----"
    (tmp_path / "fake-elf.so").write_bytes(b"\x7fELF" + header.encode())
    (tmp_path / "leaked.txt").write_text(header, encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        "FROM busybox\nCOPY fake-elf.so /fake-elf.so\nCOPY leaked.txt /leaked.txt\n",
        encoding="utf-8",
    )
    image_tag = "t18-repo-policy-elf-image"
    subprocess.run(
        ["docker", "build", "-t", image_tag, str(tmp_path)],
        check=True, capture_output=True,
    )
    try:
        violations = check_image_secrets(image_tag)
    finally:
        subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True)

    assert any("leaked.txt" in v for v in violations), (
        f"a PEM private-key header in a plain-text file must still be "
        f"caught -- the ELF skip must not become a blanket binary skip. "
        f"Violations were: {violations}"
    )
    assert not any("fake-elf.so" in v for v in violations), (
        f"content inside an ELF object must not be scanned. Violations "
        f"were: {violations}"
    )


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


@pytest.mark.docker
def test_this_projects_own_built_image_contains_no_secrets() -> None:
    """T18 whole-branch review finding I6: `check_image_secrets` was correct,
    and tested against a synthetic image with a planted secret, but was
    *never run against this project's own image* -- not by CI, not by the
    runbook, not by any test. It is deliberately excluded from the `all`
    dispatch (it needs an explicit --image), so nothing would ever have
    caught a real credential baked into a real layer.

    This runs the real check against the real tag `docker compose build`
    produces -- the same thing `infra/RUNBOOK.md`'s build step and CI's
    t18-container-security job now invoke via
    `repository_policy.py image --image personal-voice-msg:t18`.
    """

    here = Path(__file__).resolve().parent
    built = subprocess.run(
        ["docker", "compose", "-p", "personal_voice_msg_test",
         "-f", "docker-compose.yml", "build"],
        capture_output=True, text=True, timeout=1800,
        # docker-compose.yml declares SECRET_ROOT/APP_CONFIG_DIR with
        # Compose's required-variable syntax, and `build` still interpolates
        # the whole file, so both must be set to *something* that exists.
        # Neither is mounted by a build.
        env={**os.environ, "SECRET_ROOT": str(here), "APP_CONFIG_DIR": str(here)},
    )
    assert built.returncode == 0, built.stderr

    assert check_image_secrets("personal-voice-msg:t18") == [], (
        "the built application image contains something the secret scanner "
        "recognises as a credential or a sensitive artifact name"
    )

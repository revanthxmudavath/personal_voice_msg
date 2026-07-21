from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

POLICY_SCRIPT = Path(__file__).parents[2] / "scripts" / "repository_policy.py"


def run_policy(root: Path, check: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(POLICY_SCRIPT), check, "--root", str(root)],
        capture_output=True,
        check=False,
        text=True,
    )


def assert_failed_with(result: subprocess.CompletedProcess[str], *terms: str) -> None:
    output = f"{result.stdout}\n{result.stderr}".lower()
    assert result.returncode != 0
    for term in terms:
        assert term.lower() in output


def initialize_locked_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        """\
[project]
name = "policy-fixture"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["uv", "lock", "--project", str(root)],
        capture_output=True,
        check=True,
        text=True,
    )


@pytest.mark.fast
def test_mock_scan_accepts_clean_python(tmp_path: Path) -> None:
    (tmp_path / "clean.py").write_text("value = 1\n", encoding="utf-8")

    result = run_policy(tmp_path, "mocks")

    assert result.returncode == 0, result.stderr


@pytest.mark.fast
def test_mock_scan_rejects_prohibited_import(tmp_path: Path) -> None:
    planted = tmp_path / "tests" / "test_planted.py"
    planted.parent.mkdir()
    planted.write_text("from unittest import mock\n", encoding="utf-8")

    result = run_policy(tmp_path, "mocks")

    assert_failed_with(result, "mock", "test_planted.py")


@pytest.mark.fast
@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("test_invalid.py", b"def broken(:\n"),
        ("test_non_utf8.py", b"value = \xff\xfe\n"),
    ],
)
def test_mock_scan_rejects_unscannable_python(
    tmp_path: Path,
    filename: str,
    content: bytes,
) -> None:
    planted = tmp_path / "tests" / filename
    planted.parent.mkdir()
    planted.write_bytes(content)

    result = run_policy(tmp_path, "mocks")

    assert_failed_with(result, filename)


@pytest.mark.fast
@pytest.mark.parametrize(
    "source",
    [
        "from pytest import MonkeyPatch\n",
        "import pytest\npatcher = pytest.MonkeyPatch()\n",
    ],
)
def test_mock_scan_rejects_pytest_monkeypatch_usage(
    tmp_path: Path, source: str
) -> None:
    planted = tmp_path / "tests" / "test_monkeypatch.py"
    planted.parent.mkdir()
    planted.write_text(source, encoding="utf-8")

    result = run_policy(tmp_path, "mocks")

    assert_failed_with(result, "monkeypatch", "test_monkeypatch.py")


@pytest.mark.fast
@pytest.mark.parametrize(
    "source",
    [
        "import pytest\npatcher = getattr(pytest, 'MonkeyPatch')()\n",
        (
            "import importlib\n"
            "mock_module = importlib.import_module('unittest.mock')\n"
        ),
    ],
)
def test_mock_scan_rejects_dynamic_mock_access(tmp_path: Path, source: str) -> None:
    planted = tmp_path / "tests" / "test_dynamic_mock.py"
    planted.parent.mkdir()
    planted.write_text(source, encoding="utf-8")

    result = run_policy(tmp_path, "mocks")

    assert_failed_with(result, "mock", "test_dynamic_mock.py")


@pytest.mark.fast
@pytest.mark.parametrize("lock_state", ["missing", "stale"])
def test_lockfile_check_rejects_missing_or_stale_lock(
    tmp_path: Path, lock_state: str
) -> None:
    initialize_locked_project(tmp_path)
    if lock_state == "missing":
        (tmp_path / "uv.lock").unlink()
    else:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace(
                'requires-python = ">=3.12"', 'requires-python = ">=3.11"'
            ),
            encoding="utf-8",
        )

    result = run_policy(tmp_path, "lockfile")

    assert_failed_with(result, "lock", lock_state)


@pytest.mark.fast
def test_lockfile_check_accepts_current_lock(tmp_path: Path) -> None:
    initialize_locked_project(tmp_path)

    result = run_policy(tmp_path, "lockfile")

    assert result.returncode == 0, result.stderr


@pytest.mark.fast
def test_secret_scan_rejects_dummy_github_credential(tmp_path: Path) -> None:
    planted = tmp_path / "credentials.txt"
    planted.write_text(
        "token=gh" "p_abcdefghijklmnopqrstuvwxyz1234567890\n",
        encoding="utf-8",
    )

    result = run_policy(tmp_path, "secrets")

    assert_failed_with(result, "credential", "credentials.txt")


@pytest.mark.fast
def test_secret_scan_accepts_non_secret_content(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("No credentials here.\n", encoding="utf-8")

    result = run_policy(tmp_path, "secrets")

    assert result.returncode == 0, result.stderr


@pytest.mark.fast
@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("recipient.json", b'{"phone_number":"+15550001111"}\n'),
        ("waha-token.txt", b"waha-private-test-token\n"),
        ("owner.voice.embedding", b"\x00\x01private-voice-vector\xff"),
        ("waha-session.bin", b"\x00\x01private-session-state\xff"),
        ("id_ed25519", b"private-key-material"),
    ],
)
def test_secret_scan_rejects_sensitive_artifact_filenames(
    tmp_path: Path,
    filename: str,
    content: bytes,
) -> None:
    planted = tmp_path / filename
    planted.write_bytes(content)

    result = run_policy(tmp_path, "secrets")

    assert_failed_with(result, filename)


@pytest.mark.fast
def test_secret_scan_rejects_private_key_content(tmp_path: Path) -> None:
    planted = tmp_path / "deployment-notes.txt"
    private_key = (
        "-----BEGIN "
        + "PRIVATE KEY-----\n"
        + "not-a-real-key\n"
        + "-----END PRIVATE KEY-----\n"
    )
    planted.write_text(private_key, encoding="utf-8")

    result = run_policy(tmp_path, "secrets")

    assert_failed_with(result, "private key", "deployment-notes.txt")


@pytest.mark.fast
def test_secret_scan_allows_explicit_documentation_examples(tmp_path: Path) -> None:
    fixture_root = tmp_path / "tests" / "fixtures"
    fixture_root.mkdir(parents=True)
    (fixture_root / "recipient.example.json").write_text(
        '{"phone_number":"+15550001111"}\n',
        encoding="utf-8",
    )
    (fixture_root / "voice.embedding.example").write_text(
        "documented-placeholder-only\n",
        encoding="utf-8",
    )

    result = run_policy(tmp_path, "secrets")

    assert result.returncode == 0, result.stderr


@pytest.mark.fast
def test_secret_scan_accepts_current_repository_documented_fixtures() -> None:
    repository_root = Path(__file__).parents[2]

    result = run_policy(repository_root, "secrets")

    assert result.returncode == 0, result.stderr


@pytest.mark.fast
def test_workflow_check_rejects_invalid_yaml(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "invalid.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: [unterminated\n", encoding="utf-8")

    result = run_policy(tmp_path, "workflow")

    assert_failed_with(result, "invalid.yml", "yaml")


@pytest.mark.fast
def test_workflow_check_rejects_missing_workflow(tmp_path: Path) -> None:
    result = run_policy(tmp_path, "workflow")

    assert_failed_with(result, "workflow", "missing")


@pytest.mark.fast
@pytest.mark.parametrize(
    ("filename", "content", "missing_field"),
    [
        (
            "missing_trigger.yml",
            "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
            "trigger",
        ),
        ("missing_jobs.yml", "name: CI\non: [push]\n", "jobs"),
    ],
)
def test_workflow_check_rejects_missing_required_structure(
    tmp_path: Path, filename: str, content: str, missing_field: str
) -> None:
    workflow = tmp_path / ".github" / "workflows" / filename
    workflow.parent.mkdir(parents=True)
    workflow.write_text(content, encoding="utf-8")

    result = run_policy(tmp_path, "workflow")

    assert_failed_with(result, filename, missing_field)


@pytest.mark.fast
def test_workflow_check_accepts_valid_yaml(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
""",
        encoding="utf-8",
    )

    result = run_policy(tmp_path, "workflow")

    assert result.returncode == 0, result.stderr

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".uv-cache",
    ".venv",
    "__pycache__",
}
GITHUB_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)


def repository_files(root: Path, suffixes: set[str] | None = None) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(
            part in EXCLUDED_DIRECTORIES or part.startswith(".pytest-tmp-")
            for part in relative_parts
        ):
            continue
        if suffixes is not None and path.suffix.lower() not in suffixes:
            continue
        yield path


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def check_mocks(root: Path) -> list[str]:
    violations: list[str] = []
    for path in repository_files(root, {".py"}):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue

        pytest_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "pytest"
        }

        for node in ast.walk(tree):
            prohibited = False
            if isinstance(node, ast.Import):
                prohibited = any(
                    alias.name == "unittest.mock"
                    or alias.name.startswith("pytest_mock")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                prohibited = (
                    node.module == "unittest.mock"
                    or (
                        node.module == "unittest"
                        and any(alias.name == "mock" for alias in node.names)
                    )
                    or (node.module or "").startswith("pytest_mock")
                    or (
                        node.module == "pytest"
                        and any(alias.name == "MonkeyPatch" for alias in node.names)
                    )
                )
            elif isinstance(node, ast.arg):
                prohibited = node.arg == "monkeypatch"
            elif isinstance(node, ast.Attribute):
                prohibited = (
                    node.attr == "MonkeyPatch"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in pytest_aliases
                )

            if prohibited:
                violations.append(
                    f"mock or monkeypatch usage prohibited: "
                    f"{display_path(path, root)}:{getattr(node, 'lineno', 1)}"
                )
    return violations


def check_lockfile(root: Path) -> list[str]:
    if not (root / "uv.lock").is_file():
        return ["lockfile missing: uv.lock"]

    result = subprocess.run(
        ["uv", "lock", "--check", "--project", str(root)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        return []
    detail = result.stderr.strip() or result.stdout.strip()
    return [f"lockfile stale: {detail}"]


def check_secrets(root: Path) -> list[str]:
    violations: list[str] = []
    for path in repository_files(root):
        try:
            if path.stat().st_size > 1_000_000:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if GITHUB_TOKEN.search(content):
            violations.append(
                f"credential detected: {display_path(path, root)}"
            )
    return violations


def check_workflows(root: Path) -> list[str]:
    workflow_root = root / ".github" / "workflows"
    violations: list[str] = []
    paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
    if not paths:
        return ["workflow missing: .github/workflows/*.yml"]

    for path in paths:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            violations.append(
                f"workflow YAML invalid: {display_path(path, root)}: {error}"
            )
            continue
        if not isinstance(document, dict):
            violations.append(
                f"workflow YAML invalid: {display_path(path, root)} is not a mapping"
            )
            continue
        if "on" not in document and True not in document:
            violations.append(
                f"workflow trigger missing: {display_path(path, root)}"
            )
        jobs = document.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            violations.append(f"workflow jobs missing: {display_path(path, root)}")
    return violations


CHECKS = {
    "mocks": check_mocks,
    "lockfile": check_lockfile,
    "secrets": check_secrets,
    "workflow": check_workflows,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate repository policy")
    parser.add_argument("check", choices=[*CHECKS, "all"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    checks = CHECKS.values() if args.check == "all" else (CHECKS[args.check],)
    violations = [violation for check in checks for violation in check(root)]
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

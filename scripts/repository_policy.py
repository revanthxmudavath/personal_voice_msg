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
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
SENSITIVE_ARTIFACT_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "recipient.json",
}
SENSITIVE_ARTIFACT_SUFFIXES = {
    ".embedding",
    ".key",
    ".p12",
    ".pfx",
}
DOCUMENTATION_SUFFIXES = {".json", ".md", ".toml", ".txt", ".yaml", ".yml"}


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
        except (SyntaxError, UnicodeDecodeError) as error:
            violations.append(
                f"Python file cannot be scanned for mocks: "
                f"{display_path(path, root)}: {error}"
            )
            continue

        pytest_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "pytest"
        }
        importlib_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "importlib"
        }
        import_module_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "importlib"
            for alias in node.names
            if alias.name == "import_module"
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
            elif isinstance(node, ast.Call):
                arguments = node.args
                dynamic_pytest_access = (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(arguments) >= 2
                    and isinstance(arguments[0], ast.Name)
                    and arguments[0].id in pytest_aliases
                    and isinstance(arguments[1], ast.Constant)
                    and arguments[1].value == "MonkeyPatch"
                )
                module_name = (
                    arguments[0].value
                    if arguments and isinstance(arguments[0], ast.Constant)
                    else None
                )
                dynamic_import = (
                    isinstance(module_name, str)
                    and (
                        module_name == "unittest.mock"
                        or module_name.startswith("pytest_mock")
                    )
                    and (
                        (
                            isinstance(node.func, ast.Name)
                            and node.func.id == "__import__"
                        )
                        or (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "import_module"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id in importlib_aliases
                        )
                        or (
                            isinstance(node.func, ast.Name)
                            and node.func.id in import_module_aliases
                        )
                    )
                )
                indirect_monkeypatch = (
                    module_name == "monkeypatch"
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"getfixturevalue", "usefixtures"}
                )
                prohibited = (
                    dynamic_pytest_access or dynamic_import or indirect_monkeypatch
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
        filename = path.name.casefold()
        documented_example = filename.endswith(".example") or any(
            filename.endswith(f".example{suffix}")
            for suffix in DOCUMENTATION_SUFFIXES
        )
        sensitive_filename = (
            filename in SENSITIVE_ARTIFACT_NAMES
            or path.suffix.casefold() in SENSITIVE_ARTIFACT_SUFFIXES
            or ("waha" in filename and "token" in filename)
            or ("waha" in filename and "session" in filename)
        )
        if sensitive_filename and not documented_example:
            violations.append(
                f"sensitive artifact detected: {display_path(path, root)}"
            )
            continue
        try:
            content_bytes = path.read_bytes()
        except OSError:
            violations.append(
                f"file cannot be scanned for secrets: {display_path(path, root)}"
            )
            continue
        decoded_content: list[str] = []
        try:
            decoded_content.append(content_bytes.decode("utf-8"))
        except UnicodeDecodeError:
            pass
        has_utf16_shape = b"\x00" in content_bytes or content_bytes.startswith(
            (b"\xff\xfe", b"\xfe\xff")
        )
        if has_utf16_shape:
            for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
                try:
                    decoded_content.append(content_bytes.decode(encoding))
                except UnicodeDecodeError:
                    continue
        if not decoded_content:
            violations.append(
                f"file cannot be scanned for secrets: {display_path(path, root)}"
            )
            continue
        if any(GITHUB_TOKEN.search(content) for content in decoded_content):
            violations.append(
                f"credential detected: {display_path(path, root)}"
            )
        if any(PRIVATE_KEY.search(content) for content in decoded_content):
            violations.append(
                f"private key detected: {display_path(path, root)}"
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

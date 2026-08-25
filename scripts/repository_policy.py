from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

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
    "node_modules",
}
GITHUB_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
GEMINI_API_KEY = re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")
TELEGRAM_BOT_TOKEN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
SENSITIVE_ARTIFACT_NAMES = {
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "recipient.json",
    "telegram_chat_id.json",
}
SENSITIVE_ARTIFACT_SUFFIXES = {
    ".embedding",
    ".key",
    ".p12",
    ".pfx",
}
DOCUMENTATION_SUFFIXES = {".json", ".md", ".toml", ".txt", ".yaml", ".yml"}
FULL_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}")


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


def _is_documented_example(filename: str) -> bool:
    return filename.endswith(".example") or any(
        filename.endswith(f".example{suffix}") for suffix in DOCUMENTATION_SUFFIXES
    )


def _is_sensitive_filename(filename: str, suffix: str) -> bool:
    return (
        filename in SENSITIVE_ARTIFACT_NAMES
        or suffix in SENSITIVE_ARTIFACT_SUFFIXES
        or ("waha" in filename and "token" in filename)
        or ("waha" in filename and "session" in filename)
        or ("telegram" in filename and "token" in filename)
        or ("gemini" in filename and ("key" in filename or "token" in filename))
        or ("sender" in filename and "key" in filename)
    )


def _scan_content_for_secrets(content: str, label: str) -> list[str]:
    violations: list[str] = []
    if GITHUB_TOKEN.search(content):
        violations.append(f"credential detected: {label}")
    if GEMINI_API_KEY.search(content):
        violations.append(f"credential detected: {label}")
    if TELEGRAM_BOT_TOKEN.search(content):
        violations.append(f"credential detected: {label}")
    if PRIVATE_KEY.search(content):
        violations.append(f"private key detected: {label}")
    return violations


def check_secrets(root: Path) -> list[str]:
    violations: list[str] = []
    for path in repository_files(root):
        filename = path.name.casefold()
        documented_example = _is_documented_example(filename)
        sensitive_filename = _is_sensitive_filename(filename, path.suffix.casefold())
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
        if any(GEMINI_API_KEY.search(content) for content in decoded_content):
            violations.append(
                f"credential detected: {display_path(path, root)}"
            )
        if any(TELEGRAM_BOT_TOKEN.search(content) for content in decoded_content):
            violations.append(
                f"credential detected: {display_path(path, root)}"
            )
        if any(PRIVATE_KEY.search(content) for content in decoded_content):
            violations.append(
                f"private key detected: {display_path(path, root)}"
            )
    return violations


def check_git_history(root: Path) -> list[str]:
    violations: list[str] = []
    listing = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--all", "--objects"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in listing.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        blob_sha, path_in_history = parts
        filename = Path(path_in_history).name.casefold()
        documented_example = _is_documented_example(filename)
        sensitive_filename = _is_sensitive_filename(
            filename, Path(path_in_history).suffix.casefold()
        )
        if sensitive_filename and not documented_example:
            violations.append(
                f"sensitive artifact detected in history: "
                f"{path_in_history}@{blob_sha[:12]}"
            )
            continue
        content = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-p", blob_sha],
            capture_output=True,
            text=True,
            errors="ignore",
            check=False,
        ).stdout
        violations.extend(
            v.replace(path_in_history, f"{path_in_history}@{blob_sha[:12]}")
            for v in _scan_content_for_secrets(content, path_in_history)
        )
    return violations


def check_image_secrets(image: str) -> list[str]:
    violations: list[str] = []
    container = subprocess.run(
        ["docker", "create", image],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    try:
        export = subprocess.run(
            ["docker", "export", container], capture_output=True, check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(export.stdout)) as archive:
            for member in archive.getmembers():
                # Only regular files hold scannable content; symlinks (some
                # point at absolute paths outside any extraction root, e.g.
                # /etc/mtab), directories, and device/fifo entries are
                # skipped rather than extracted to disk.
                if not member.isfile():
                    continue
                label = member.name
                filename = Path(label).name.casefold()
                documented_example = _is_documented_example(filename)
                sensitive_filename = _is_sensitive_filename(
                    filename, Path(label).suffix.casefold()
                )
                if sensitive_filename and not documented_example:
                    violations.append(
                        f"sensitive artifact detected in image: {label}"
                    )
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                content = extracted.read().decode("utf-8", errors="ignore")
                violations.extend(_scan_content_for_secrets(content, label))
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    return violations


def _is_immutable_uses(reference: object) -> bool:
    if not isinstance(reference, str) or not reference.strip():
        return False
    if reference.startswith("./"):
        return True
    _, separator, revision = reference.rpartition("@")
    return bool(separator and FULL_COMMIT_SHA.fullmatch(revision))


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
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                violations.append(
                    f"workflow job invalid: {display_path(path, root)}: "
                    f"{job_name} is not a mapping"
                )
                continue

            reusable_workflow = job.get("uses")
            if reusable_workflow is not None:
                if not _is_immutable_uses(reusable_workflow):
                    violations.append(
                        f"workflow uses must be local or immutable: "
                        f"{display_path(path, root)}: {job_name}"
                    )
                continue

            if "runs-on" not in job:
                violations.append(
                    f"workflow job runs-on missing: "
                    f"{display_path(path, root)}: {job_name}"
                )
            steps = job.get("steps")
            if not isinstance(steps, list) or not steps:
                violations.append(
                    f"workflow job steps missing: "
                    f"{display_path(path, root)}: {job_name}"
                )
                continue
            for index, step in enumerate(steps, start=1):
                executable = isinstance(step, dict) and any(
                    isinstance(step.get(command), str) and step[command].strip()
                    for command in ("run", "uses")
                )
                if not executable:
                    violations.append(
                        f"workflow step invalid: {display_path(path, root)}: "
                        f"{job_name} step {index}"
                    )
                    continue
                if "uses" in step and not _is_immutable_uses(step["uses"]):
                    violations.append(
                        f"workflow uses must be local or immutable: "
                        f"{display_path(path, root)}: {job_name} step {index}"
                    )
    return violations


CHECKS = {
    "mocks": check_mocks,
    "lockfile": check_lockfile,
    "secrets": check_secrets,
    "workflow": check_workflows,
    "git-history": check_git_history,
    "image": check_image_secrets,
}
# "image" scans a built Docker image tag rather than a repository root, so it
# cannot run as part of "all" (there is no default image to scan) and is
# dispatched with --image instead of --root in main().
IMAGE_CHECKS = {"image"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate repository policy")
    parser.add_argument("check", choices=[*CHECKS, "all"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--image", default=None, help="image tag to scan (required for 'image')"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    names = (
        [name for name in CHECKS if name not in IMAGE_CHECKS]
        if args.check == "all"
        else [args.check]
    )
    violations: list[str] = []
    for name in names:
        if name in IMAGE_CHECKS:
            if not args.image:
                print(f"--image is required for the '{name}' check", file=sys.stderr)
                return 2
            image_check = cast(Callable[[str], list[str]], CHECKS[name])
            violations.extend(image_check(args.image))
        else:
            root_check = cast(Callable[[Path], list[str]], CHECKS[name])
            violations.extend(root_check(root))
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

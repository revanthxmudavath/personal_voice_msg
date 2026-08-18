from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "personal_voice_msg"
RESTRICTED_PACKAGES = ("discovery", "generation", "judging")
FORBIDDEN_MODULES = {
    "pocket_tts",
    "personal_voice_msg.voice_enrollment",
    "personal_voice_msg.sender",
}
FORBIDDEN_CONFIG_IMPORTS = {"Settings", "load_settings"}
# T15 extends this boundary to the sender's own secrets, per AGENTS.md's
# T13 entry ("scoped to currently-existing code only, since the sender
# doesn't exist until T15/T16, which must extend this same check when
# built") -- see docs/task-logs/T15.md.
FORBIDDEN_ATTRIBUTE_NAMES = {"voice_embedding", "sender_auth_key", "telegram_bot_token"}


def _iter_source_files(package: str) -> list[Path]:
    return [
        path
        for path in (SRC_ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def _find_violations(tree: ast.AST, path: Path) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    violations.append(f"{path}: imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in FORBIDDEN_MODULES:
                violations.append(f"{path}: imports from {module}")
            if module == "personal_voice_msg.config":
                for alias in node.names:
                    if alias.name in FORBIDDEN_CONFIG_IMPORTS:
                        violations.append(
                            f"{path}: imports {alias.name} from {module}"
                        )
        elif isinstance(node, (ast.Attribute, ast.Name)):
            attr_name = getattr(node, "attr", getattr(node, "id", None))
            if attr_name in FORBIDDEN_ATTRIBUTE_NAMES:
                violations.append(f"{path}: references {attr_name}")
    return violations


@pytest.mark.parametrize("package", RESTRICTED_PACKAGES)
def test_restricted_package_cannot_read_voice_or_sender_secrets(package: str) -> None:
    violations: list[str] = []
    for path in _iter_source_files(package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_find_violations(tree, path))

    assert violations == []

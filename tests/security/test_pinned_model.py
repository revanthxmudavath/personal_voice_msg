from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

GEMINI_MODEL = "gemini-3.6-flash"


def test_generation_module_still_pins_the_qualified_gemini_model() -> None:
    """AGENTS.md documents gemini-3.6-flash as the T10/T11-qualified pin.
    Fails if a future edit changes the model string without a matching
    requalification -- catch drift at CI time, not in production."""
    generation_source = Path("src/personal_voice_msg/generation").rglob("*.py")
    found = any(
        GEMINI_MODEL in path.read_text(encoding="utf-8") for path in generation_source
    )
    assert found, (
        f"expected the pinned model {GEMINI_MODEL!r} somewhere under "
        "src/personal_voice_msg/generation/ -- if this changed intentionally, "
        "it requires full T10/T11 requalification per AGENTS.md, not a quiet edit"
    )

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "run_daily_entrypoint.py"


@pytest.mark.fast
def test_run_daily_entrypoint_script_requires_config_and_database() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "--config" in result.stderr
    assert "--database" in result.stderr


@pytest.mark.fast
def test_run_daily_entrypoint_script_requires_database_when_only_config_given(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(tmp_path / "settings.toml")],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert "--database" in result.stderr

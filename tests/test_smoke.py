from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import cgate
from cgate.core.paths import data_dir, db_path


def test_version_string() -> None:
    assert cgate.__version__ == "0.1.0"


def test_data_dir_is_path() -> None:
    assert isinstance(data_dir(), Path)


def test_db_path_under_data_dir() -> None:
    assert db_path().parent == data_dir()


def test_cli_version_subprocess(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "APPDATA": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, "-m", "cgate", "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert "0.1.0" in result.stdout

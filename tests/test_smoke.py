from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import cgate
from cgate.core.paths import data_dir, db_path


def test_version_string() -> None:
    # The version is derived from the git tag (or a fallback) at build time,
    # so we only assert the PEP 440 base shape rather than pinning a literal.
    # Hatch-vcs produces dev segments like "0.1.6.dev1+g<hash>.d<date>" for
    # off-tag commits; the regex allows any non-whitespace suffix.
    assert re.fullmatch(r"\d+\.\d+\.\d+\S*", cgate.__version__), cgate.__version__


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
    assert re.search(r"^cgate \d+\.\d+\.\d+", result.stdout), result.stdout

"""Smoke-test the platform-native PyInstaller binary."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ = pytest.importorskip("PyInstaller")


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - commands are constructed solely by this test
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_built_binary_exposes_the_complete_cli(tmp_path: Path) -> None:
    # Given: an isolated PyInstaller workspace and cgate data directory.
    project_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "APPDATA": str(tmp_path),
        "CGATE_BUILD_WORKPATH": str(tmp_path / "pyinstaller"),
        "CGATE_DB_PATH": str(tmp_path / "cgate.db"),
        "HOME": str(tmp_path),
        "LOCALAPPDATA": str(tmp_path),
    }

    try:
        # When: the release binary is built and each public CLI surface is invoked.
        build = _run(
            [sys.executable, "scripts/build-binary.py"],
            cwd=project_root,
            env=env,
        )
        assert build.returncode == 0, f"{build.stdout}\n{build.stderr}"
        binary_lines = [
            line.removeprefix("Binary: ").strip()
            for line in build.stdout.splitlines()
            if line.startswith("Binary: ")
        ]
        assert len(binary_lines) == 1, build.stdout
        binary = Path(binary_lines[0])
        assert binary.is_file()

        version = _run([str(binary), "--version"], cwd=project_root, env=env)
        root_help = _run([str(binary), "--help"], cwd=project_root, env=env)
        connections_help = _run(
            [str(binary), "connections", "--help"], cwd=project_root, env=env
        )
        connections_list = _run(
            [str(binary), "connections", "list"], cwd=project_root, env=env
        )

        # Then: the binary exposes version, command groups, and database-backed behavior.
        assert version.returncode == 0, version.stderr
        # The version baked into the PyInstaller binary comes from the nearest
        # git tag at build time (see scripts/_ensure_version.py); assert shape
        # only so this test stays valid for every release.
        assert re.search(r"^cgate \d+\.\d+\.\d+", version.stdout), version.stdout
        assert root_help.returncode == 0, root_help.stderr
        assert all(command in root_help.stdout for command in ("connections", "watch"))
        assert connections_help.returncode == 0, connections_help.stderr
        assert connections_list.returncode == 0, connections_list.stderr
        assert "No connections saved." in connections_list.stdout
    finally:
        shutil.rmtree(project_root / "dist", ignore_errors=True)

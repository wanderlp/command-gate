"""Build a single-file cgate executable for the current platform.

Run with ``uv run python scripts/build-binary.py`` from the project root.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
DIST_DIR: Final = PROJECT_ROOT / "dist"
ENSURE_VERSION_SCRIPT: Final = PROJECT_ROOT / "scripts" / "_ensure_version.py"


def main() -> int:
    """Run PyInstaller and rename its output to the release asset name."""
    system = platform.system()
    machine = platform.machine().lower()
    match (system, machine):
        case ("Windows", "amd64" | "x86_64"):
            asset_name = "cgate-windows-amd64.exe"
            executable_name = "cgate.exe"
        case ("Darwin", "arm64" | "aarch64"):
            asset_name = "cgate-macos-arm64"
            executable_name = "cgate"
        case ("Linux", "amd64" | "x86_64"):
            asset_name = "cgate-linux-x86_64"
            executable_name = "cgate"
        case _:
            print(f"Unsupported build platform: {system} {platform.machine()}", file=sys.stderr)
            return 2

    configured_workpath = os.environ.get("CGATE_BUILD_WORKPATH")
    owns_workpath = configured_workpath is None
    workpath = (
        Path(tempfile.mkdtemp(prefix="cgate-pyinstaller-"))
        if owns_workpath
        else Path(configured_workpath)
    ).resolve()
    specpath = workpath.with_name(f"{workpath.name}-spec")
    workpath.mkdir(parents=True, exist_ok=True)
    specpath.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name=cgate",
        "--clean",
        "--noconfirm",
        f"--distpath={DIST_DIR}",
        f"--workpath={workpath / 'build'}",
        f"--specpath={specpath}",
        f"--paths={PROJECT_ROOT / 'src'}",
        "--collect-all=keyring",
        "--collect-all=mcp",
        "--collect-all=paramiko",
        "--collect-all=pywinrm",
        "--collect-all=rich",
        "--hidden-import=cgate.cli.connections",
        "--hidden-import=cgate.cli.mcp",
        "--hidden-import=cgate.cli.watch",
        "--hidden-import=cgate.mcp_server",
        str(PROJECT_ROOT / "src" / "cgate" / "__main__.py"),
    ]

    try:
        version_step = subprocess.run(
            [sys.executable, str(ENSURE_VERSION_SCRIPT)],
            cwd=PROJECT_ROOT,
            check=False,
        )
        if version_step.returncode != 0:
            return version_step.returncode

        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if completed.returncode != 0:
            return completed.returncode

        produced = DIST_DIR / executable_name
        target = DIST_DIR / asset_name
        if target.exists():
            target.unlink()
        shutil.move(produced, target)
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"Binary: {target.resolve()}")
        print(f"Size: {size_mb:.2f} MB")
        return 0
    finally:
        if owns_workpath:
            shutil.rmtree(workpath, ignore_errors=True)
            shutil.rmtree(specpath, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

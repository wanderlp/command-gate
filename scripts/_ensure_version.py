"""Ensure src/cgate/_version.py exists with a valid __version__ before PyInstaller.

PyInstaller does not run hatchling build hooks, so we mirror hatch-vcs's
behavior here: when `uv sync --extra dev` has already produced _version.py,
we leave it alone. When it is missing (e.g., a `python scripts/build-binary.py`
invocation from a fresh checkout that skipped `uv sync`), we fall back to
setuptools_scm -- the same library hatch-vcs delegates to -- so the version
shape stays consistent with what `pip install` users see.

Idempotent: re-running with a current _version.py is a no-op.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
VERSION_FILE: Path = PROJECT_ROOT / "src" / "cgate" / "_version.py"

_VERSION_RE = re.compile(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)


def _read_existing_version() -> str | None:
    if not VERSION_FILE.exists():
        return None
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def _compute_version_with_setuptools_scm() -> str | None:
    try:
        from setuptools_scm import get_version
    except ImportError:
        return None
    try:
        return get_version(root=str(PROJECT_ROOT), dist_name="command-gate")
    except Exception as exc:  # noqa: BLE001 - setuptools_scm raises varied types
        print(f"setuptools_scm failed: {exc}", file=sys.stderr)
        return None


def _compute_version_with_git() -> str | None:
    """Last-resort fallback: read the nearest matching tag without local segment."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match=v*"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    tag = result.stdout.strip()
    return tag[1:] if tag.startswith("v") else None


def main() -> int:
    existing = _read_existing_version()
    if existing is not None:
        print(f"{VERSION_FILE} already has __version__={existing}; not regenerating")
        return 0

    version = _compute_version_with_setuptools_scm() or _compute_version_with_git()
    if version is None:
        print(
            "ERROR: could not derive version from git and setuptools_scm "
            "is unavailable. Run `uv sync --extra dev` first.",
            file=sys.stderr,
        )
        return 1

    body = (
        '"""Auto-generated from git; do not edit by hand."""\n'
        f'__version__ = "{version}"\n'
    )
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(body, encoding="utf-8")
    print(f"Wrote {VERSION_FILE} (version={version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

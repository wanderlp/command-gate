"""Per-user storage paths for command-gate."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """Return the OS-appropriate per-user data directory for command-gate."""
    app = "command-gate"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base is None:
            base = str(Path.home() / "AppData" / "Local")
        return Path(base) / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / app
    return Path.home() / ".local" / "share" / app


def db_path() -> Path:
    """Return the path to the SQLite database.

    Overridable via the CGATE_DB_PATH environment variable (used by tests
    and by operators who want to point at an alternate location).
    """
    override = os.environ.get("CGATE_DB_PATH")
    if override:
        return Path(override)
    return data_dir() / "cgate.db"

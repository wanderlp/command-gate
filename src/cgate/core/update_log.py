"""Best-effort append-only logging for background update/uninstall operations.

Lives in ``cgate.core`` (not ``cgate.cli``) because it's shared by the
interactive CLI and the non-interactive ``cgate.helper`` binary, which must
not import anything from ``cgate.cli`` (typer/rich/mcp/paramiko/pywinrm).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import cgate.core.paths

if TYPE_CHECKING:
    from pathlib import Path

_LOG_FILENAME = "update.log"

# Cap the log at 1 MiB. The log is purely diagnostic -- no sensitive or
# untrusted content is interpolated -- but on a long-running install with
# frequent `cgate update check`/`apply` calls (or any heal/retry loop that
# logs on each attempt), the file would otherwise grow indefinitely. 1 MiB
# is "a few MB" off the issue's suggested range, large enough to retain a
# useful debugging window (~10k entries at ~100 bytes each) while bounding
# disk usage on a per-user install. The cap is checked after every write;
# when it is exceeded, the oldest lines are dropped until the file fits
# with ~25% headroom so we do not rotate on every subsequent write.
_MAX_LOG_BYTES = 1 * 1024 * 1024
_ROTATE_TARGET_BYTES = int(_MAX_LOG_BYTES * 0.75)


def append_log(message: str) -> None:
    """Append a timestamped line to ``<data_dir>/update.log``. Never raises.

    Best-effort: any failure (permission denied, missing dir, encoding
    errors) is silently swallowed. Logging must never crash the caller --
    in particular, the helper process that runs after the main cgate
    process has exited has no other way to surface failure.

    Uses ``cgate.core.paths.data_dir`` (attribute lookup at call time)
    rather than a module-level import so tests can monkeypatch the data
    directory location.
    """
    try:
        log_path = cgate.core.paths.data_dir() / _LOG_FILENAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as f:
            _ = f.write(f"[{timestamp}] {message}\n")
        _enforce_size_cap(log_path)
    except Exception:  # noqa: BLE001 - best-effort, see module docstring
        pass


def _enforce_size_cap(log_path: Path) -> None:
    """If the log has grown past ``_MAX_LOG_BYTES``, keep only the most recent content that fits.

    Called after every write, so the cap is enforced promptly. Reads and
    rewrites the whole file -- acceptable because the cap is small (~1 MiB)
    and rotation only happens once that cap has been exceeded.

    Best-effort: any failure (file missing, encoding error, permission
    denied, disk full) is silently swallowed. ``append_log`` is itself a
    best-effort path, and a rotation that fails must not crash the caller
    or surface to the user -- the next call will simply try again.
    """
    try:
        size = log_path.stat().st_size
        if size <= _MAX_LOG_BYTES:
            return
        text = log_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        while lines and _utf8_byte_size(lines) > _ROTATE_TARGET_BYTES:
            _ = lines.pop(0)
        _ = log_path.write_text("".join(lines), encoding="utf-8")
    except Exception:  # noqa: BLE001, S110 - best-effort, see module docstring
        pass


def _utf8_byte_size(lines: list[str]) -> int:
    """Sum the UTF-8 byte length of every line. Matches what ``stat().st_size`` reports.

    Computing the byte size directly (rather than ``sum(len(line) for line in lines)``,
    which counts code points and would diverge for non-ASCII content) keeps the
    cap check faithful to the filesystem measurement used in the size guard.
    """
    return sum(len(line.encode("utf-8")) for line in lines)


__all__ = ["append_log"]


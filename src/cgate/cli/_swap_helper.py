"""Best-effort append-only logging for ``cgate update apply``.

Lives in its own module so it can be imported without dragging in the
rest of ``cgate.cli`` (helpful for tests and for callers that only
need to write a log line).
"""

from __future__ import annotations

from datetime import UTC, datetime

import cgate.core.paths

_LOG_FILENAME = "update.log"


def append_log(message: str) -> None:
    """Append a timestamped line to ``<data_dir>/update.log``. Never raises.

    Best-effort: any failure (permission denied, missing dir, encoding
    errors) is silently swallowed. Logging must never crash the caller
    -- in particular, the helper process that runs after the main
    cgate process has exited has no other way to surface failure.

    Uses ``cgate.core.paths.data_dir`` (attribute lookup at call time)
    rather than a module-level import so tests can monkeypatch the
    data directory location.
    """
    try:
        log_path = cgate.core.paths.data_dir() / _LOG_FILENAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass

"""SQLite connection wrapper and schema initialization."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cgate.db.schema import SCHEMA_SQL, SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class Database:
    """A reference to a SQLite database file on disk."""

    path: Path


@contextmanager
def connect(database: Database) -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection. Commits on clean exit, rolls back on exception.

    Sets row_factory=sqlite3.Row for column-name access, and PRAGMA foreign_keys=ON
    so FK references in the schema are enforced.
    """
    conn = sqlite3.connect(database.path)
    conn.row_factory = sqlite3.Row
    _ = conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(database: Database) -> None:
    """Create parent dirs (if needed) and apply the schema; idempotent.

    Uses ``INSERT OR IGNORE`` rather than a SELECT-then-INSERT (issue
    #11): two ``cgate`` processes launched concurrently against a brand
    new database file could otherwise both pass the SELECT before either
    committed, then collide on the second's INSERT into the
    ``version`` PRIMARY KEY. ``OR IGNORE`` makes the row idempotent in a
    single statement instead.
    """
    database.path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database) as conn:
        _ = conn.executescript(SCHEMA_SQL)
        applied_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        _ = conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, applied_at),
        )

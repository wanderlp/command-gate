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

# Auto-approve mode tables (feature: modes + per-server opt-in). Kept out of
# schema.py's Phase-1 SCHEMA_SQL so the original schema constant stays frozen;
# both statements are IF NOT EXISTS, so init stays idempotent. No default
# app_mode row is seeded: absence means "unset" and AppModeRepo.get() raises.
_MODE_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS app_mode (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL CHECK (mode IN ('propose','auto')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS server_settings (
    server_alias TEXT PRIMARY KEY,
    auto_allowed INTEGER NOT NULL DEFAULT 0 CHECK (auto_allowed IN (0,1)),
    updated_at TEXT,
    updated_by TEXT
);
"""


@dataclass(frozen=True, slots=True)
class Database:
    """A reference to a SQLite database file on disk."""

    path: Path


@contextmanager
def connect(database: Database) -> Generator[sqlite3.Connection, None, None]:
    """Open a SQLite connection. Commits on clean exit, rolls back on exception.

    Sets row_factory=sqlite3.Row for column-name access and PRAGMA foreign_keys=ON
    so FK references in the schema are enforced. ``PRAGMA journal_mode=WAL`` is
    applied by ``init_database`` rather than here -- WAL is sticky on the
    database file, so a single enable at init time covers every subsequent
    connection (including one opened by a different process).

    ``timeout=30`` (sqlite3's default is 5s) because `cgate watch` and the
    MCP server backing an AI agent's AUTO-mode auto-execution are two
    separate processes that can now write to the same file at the same
    time -- a short busy-timeout would surface as a spurious "database is
    locked" error in the TUI under nothing worse than ordinary contention.
    """
    conn = sqlite3.connect(database.path, timeout=30)
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


def _enable_wal_mode(database: Database) -> None:
    """Ensure the database file is in WAL journal mode, on a one-shot connection.

    `PRAGMA journal_mode = WAL` writes a WAL header into the database file --
    an operation that, combined with the kind of DDL/DML sequence
    ``init_database`` runs in the main ``connect`` context, can leave Python's
    sqlite3 module in a state where the final ``conn.commit()`` raises
    ``OperationalError: cannot commit transaction - SQL statements in
    progress``. Setting the pragma here on a dedicated short-lived connection
    (and committing it immediately) sidesteps that interaction entirely.

    The pragma is sticky on the database file, so every subsequent
    connection -- including one opened by a different process against a DB
    that was previously in rollback-journal mode -- inherits WAL mode
    without us having to apply the pragma again. Existing pre-WAL databases
    are silently upgraded on the next ``init_database`` call, with no
    schema_version bump or data migration needed.
    """
    database.path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database.path, timeout=30)
    try:
        _ = conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, *, table: str, column: str, sql_type: str) -> None:
    """Add one column to an existing table if it isn't already there.

    Unlike the ``CREATE TABLE IF NOT EXISTS`` statements everywhere else
    in this file, SQLite's ``ALTER TABLE ... ADD COLUMN`` has no
    ``IF NOT EXISTS`` form, so idempotency has to be a live
    ``PRAGMA table_info`` check instead of a fixed SQL string.
    """
    # table/column/sql_type are always fixed literals from call sites below, never user input.
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        _ = conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _widen_commands_status_check_if_needed(conn: sqlite3.Connection) -> None:
    """Recreate the commands table to add ``executing`` to its status CHECK, once.

    Schema v3 (issues #36/#38) introduces the EXECUTING status. SQLite has
    no ``ALTER TABLE ... ALTER CHECK`` -- the only way to widen a CHECK
    constraint is to recreate the table, following the standard SQLite
    pattern (CREATE TABLE new / INSERT FROM old / RENAME new -> old).
    Foreign keys are suspended for the duration so the temporary table
    rename does not break FK enforcement on readers.

    Idempotent: the existing CREATE TABLE statement is inspected first;
    if it already names ``'executing'`` (fresh install, or a DB that
    previously went through this migration), the function is a no-op.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'commands'"
    ).fetchone()
    if row is not None and "'executing'" in str(row["sql"]):
        return
    _ = conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE commands_new (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(id),
            position INTEGER NOT NULL,
            server_alias TEXT NOT NULL,
            server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
            command TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'approved', 'rejected', 'executing', 'executed', 'failed')
            ),
            result TEXT,
            approved_by TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            claimed_at TEXT,
            UNIQUE (batch_id, position)
        );
        INSERT INTO commands_new
            (id, batch_id, position, server_alias, server_type, command,
             status, result, approved_by, created_at, resolved_at, claimed_at)
        SELECT
            id, batch_id, position, server_alias, server_type, command,
            status, result, approved_by, created_at, resolved_at, NULL
        FROM commands;
        DROP TABLE commands;
        ALTER TABLE commands_new RENAME TO commands;
        CREATE INDEX IF NOT EXISTS idx_commands_batch_id ON commands(batch_id);
        CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
        PRAGMA foreign_keys = ON;
        """
    )


def init_database(database: Database) -> None:
    """Create parent dirs (if needed) and apply the schema; idempotent.

    Uses ``INSERT OR IGNORE`` rather than a SELECT-then-INSERT (issue
    #11): two ``cgate`` processes launched concurrently against a brand
    new database file could otherwise both pass the SELECT before either
    committed, then collide on the second's INSERT into the
    ``version`` PRIMARY KEY. ``OR IGNORE`` makes the row idempotent in a
    single statement instead.

    Also enables WAL journal mode on the database file (issue #42). See
    ``_enable_wal_mode`` for why the pragma runs on its own connection
    rather than inside ``connect``.
    """
    database.path.parent.mkdir(parents=True, exist_ok=True)
    _enable_wal_mode(database)
    with connect(database) as conn:
        _ = conn.executescript(SCHEMA_SQL)
        _ = conn.executescript(_MODE_SETTINGS_SQL)
        _widen_commands_status_check_if_needed(conn)
        _ensure_column(conn, table="commands", column="reason", sql_type="TEXT")
        _ensure_column(conn, table="commands", column="risk_label", sql_type="TEXT")
        _ensure_column(conn, table="commands", column="claimed_at", sql_type="TEXT")
        applied_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        _ = conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, applied_at),
        )

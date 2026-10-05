"""Regression tests for the v2 -> v3 schema migration (issues #36/#38).

The EXECUTING status and the ``claimed_at`` column are introduced on
existing databases via two coordinated steps:

  1. ``_widen_commands_status_check_if_needed`` recreates the ``commands``
     table to widen the ``status`` CHECK constraint to include
     ``'executing'``. SQLite has no ``ALTER TABLE ... ALTER CHECK``.
  2. ``_ensure_column`` adds the ``claimed_at`` column idempotently.

Both run on every ``init_database`` call but are no-ops on already-migrated
DBs. The tests below exercise both shapes: a fresh DB (no migration
needed) and a synthetic old-shape DB (migration must run).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, connect, init_database
from cgate.db.schema import SCHEMA_VERSION
from cgate.db.types import CommandId, CommandStatus, ServerType

if TYPE_CHECKING:
    from pathlib import Path


def _sqlite_master_for_commands(conn: sqlite3.Connection) -> str:
    """Return the CREATE TABLE statement for the commands table, normalized."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
    ).fetchone()
    assert row is not None, "commands table does not exist"
    return str(row["sql"]).replace("\n", " ")
    """Return the CREATE TABLE statement for the commands table, normalized."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
    ).fetchone()
    assert row is not None, "commands table does not exist"
    return str(row["sql"]).replace("\n", " ")


def test_init_database_fresh_install_has_executing_status_in_check(tmp_path: Path) -> None:
    """Fresh DB: the CREATE TABLE includes 'executing' from the start."""
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    with connect(db) as conn:
        create_sql = _sqlite_master_for_commands(conn)
    assert "'executing'" in create_sql


def test_init_database_fresh_install_has_claimed_at_column(tmp_path: Path) -> None:
    """Fresh DB: claimed_at column is in the table from the start."""
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    with connect(db) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(commands)")}
    assert "claimed_at" in cols


def test_init_database_records_v3_schema_version(tmp_path: Path) -> None:
    """Fresh DB: the SCHEMA_VERSION row is 3."""
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    with connect(db) as conn:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
    assert row is not None, "schema_version row not recorded for current version"


def test_init_database_is_idempotent(tmp_path: Path) -> None:
    """Calling init_database twice does not re-run the migration."""
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    with connect(db) as conn:
        sql_after_first = _sqlite_master_for_commands(conn)
    init_database(db)
    with connect(db) as conn:
        sql_after_second = _sqlite_master_for_commands(conn)
    # Same CREATE TABLE statement both calls. _widen_commands_status_check_if_needed
    # returned without running executescript because the CHECK already matches.
    assert sql_after_first == sql_after_second


def test_old_shape_db_is_migrated_to_v3(tmp_path: Path) -> None:
    """Synthetic pre-v2 DB: init_database must widen the CHECK and add the column."""
    db_path = tmp_path / "legacy.db"
    # Build a v2-shaped DB by hand: the original CREATE TABLE (no
    # 'executing' in CHECK, no claimed_at column). The schema_version
    # row says version=2 so init_database does NOT bail out assuming it
    # has already migrated.
    with sqlite3.connect(db_path) as raw:
        _ = raw.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                requested_by_agent TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                position INTEGER NOT NULL,
                server_alias TEXT NOT NULL,
                server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
                command TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'executed', 'failed')
                ),
                result TEXT,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE (batch_id, position)
            );
            CREATE TABLE connections (
                alias TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
                detection_ssh INTEGER NOT NULL,
                detection_winrm INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()

    # Pre-condition: the legacy CHECK is in place and claimed_at does not exist.
    with sqlite3.connect(db_path) as raw:
        sql_before = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
        ).fetchone()[0]
    assert "'executing'" not in sql_before
    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        cols_before = {row["name"] for row in raw.execute("PRAGMA table_info(commands)")}
    assert "claimed_at" not in cols_before

    # Migration runs as part of init_database.
    init_database(Database(path=db_path))

    # Post-condition: the CHECK now includes 'executing', and the column exists.
    with sqlite3.connect(db_path) as raw:
        sql_after = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
        ).fetchone()[0]
    assert "'executing'" in sql_after
    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        cols_after = {row["name"] for row in raw.execute("PRAGMA table_info(commands)")}
    assert "claimed_at" in cols_after
    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        version_row = raw.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
    assert version_row["version"] == SCHEMA_VERSION, (
        "schema_version row should be updated to v3 after migration"
    )


def test_old_shape_db_preserves_existing_command_data(tmp_path: Path) -> None:
    """Migration must not lose any existing command rows.

    The recreation step is ``INSERT INTO commands_new SELECT * FROM commands``
    plus a constant ``NULL`` for ``claimed_at`` (the column didn't exist on
    the old schema). Every pre-existing row must survive with the same
    id, batch_id, position, command, and resolved_at.
    """
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as raw:
        _ = raw.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                requested_by_agent TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                position INTEGER NOT NULL,
                server_alias TEXT NOT NULL,
                server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
                command TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'executed', 'failed')
                ),
                result TEXT,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE (batch_id, position)
            );
            INSERT INTO batches
    (id, title, created_at)
VALUES
    ('b1', 'lot', '2026-01-01T00:00:00Z');
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type, command, status, created_at)
            VALUES
                ('c1', 'b1', 0, 'srv', 'linux', 'uptime', 'pending', '2026-01-01T00:00:00Z'),
                ('c2', 'b1', 1, 'srv', 'linux', 'whoami', 'approved', '2026-01-01T00:00:01Z'),
                ('c3', 'b1', 2, 'srv', 'linux', 'hostname', 'executed', '2026-01-01T00:00:02Z');
            UPDATE commands SET resolved_at = '2026-01-01T00:00:03Z' WHERE id = 'c3';
            INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()

    init_database(Database(path=db_path))

    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        rows = raw.execute(
            "SELECT id, status, resolved_at, claimed_at FROM commands ORDER BY position"
        ).fetchall()
    assert [r["id"] for r in rows] == ["c1", "c2", "c3"]
    assert [r["status"] for r in rows] == ["pending", "approved", "executed"]
    assert rows[0]["resolved_at"] is None  # pending had no resolved_at
    assert rows[1]["resolved_at"] is None  # approved had no resolved_at
    assert rows[2]["resolved_at"] == "2026-01-01T00:00:03Z"  # preserved
    assert all(r["claimed_at"] is None for r in rows), (
        "claimed_at must be NULL for migrated rows (the column didn't exist)"
    )


def test_migrated_db_supports_executing_status(tmp_path: Path) -> None:
    """Post-migration: a command can transition PENDING -> APPROVED ->
    EXECUTING and persist with claimed_at. Without the migration this
    transition would fail the CHECK constraint.
    """
    db_path = tmp_path / "legacy.db"
    # First build a v2 DB.
    with sqlite3.connect(db_path) as raw:
        _ = raw.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                requested_by_agent TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                position INTEGER NOT NULL,
                server_alias TEXT NOT NULL,
                server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
                command TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'executed', 'failed')
                ),
                result TEXT,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE (batch_id, position)
            );
            INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()

    # Migrate.
    db = Database(path=db_path)
    init_database(db)

    # Now exercise the new state machine through the repository.
    batches_repo = BatchesRepo(db)
    batch = batches_repo.create(title="lot", description=None, requested_by_agent=None)
    commands_repo = CommandsRepo(db)
    cmd = commands_repo.add(
        batch_id=batch.id,
        server_alias="srv",
        server_type=ServerType.LINUX,
        command="uptime",
    )
# Transition through the state machine the migration enables.
    assert commands_repo.update_status(cmd.id, status=CommandStatus.APPROVED)
    assert commands_repo.update_status(
        cmd.id,
        status=CommandStatus.EXECUTING,
        claimed_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    fetched = commands_repo.get(CommandId(cmd.id))
    assert fetched is not None
    assert fetched.status is CommandStatus.EXECUTING
    assert fetched.claimed_at == datetime(2026, 10, 2, tzinfo=UTC)


def test_migration_preserves_reason_and_risk_label(tmp_path: Path) -> None:
    """A v2 DB that has been in production long enough to have gone
    through ``_ensure_column('reason')`` and ``_ensure_column('risk_label')``
    has both columns. The v3 migration's ``INSERT ... SELECT`` must
    preserve them -- the original migration dropped them, which would
    silently nuke every command annotation in every production install.
    """
    db_path = tmp_path / "legacy.db"
    # Build a v2 DB that has reason + risk_label (simulating an
    # install that ran the earlier _ensure_column patches).
    with sqlite3.connect(db_path) as raw:
        _ = raw.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                requested_by_agent TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                position INTEGER NOT NULL,
                server_alias TEXT NOT NULL,
                server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
                command TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'executed', 'failed')
                ),
                result TEXT,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                reason TEXT,
                risk_label TEXT,
                UNIQUE (batch_id, position)
            );
            INSERT INTO batches
                (id, title, created_at)
            VALUES
                ('b1', 'lot', '2026-01-01T00:00:00Z');
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type, command,
                 status, reason, risk_label, created_at)
            VALUES
                ('c1', 'b1', 0, 'srv', 'linux', 'rm -rf /',
                 'pending', 'cleanup before deploy', 'recursive force delete', '2026-01-01T00:00:00Z'),
                ('c2', 'b1', 1, 'srv', 'linux', 'whoami',
                 'approved', 'check identity', NULL, '2026-01-01T00:00:01Z');
            INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()

    init_database(Database(path=db_path))

    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        rows = raw.execute(
            "SELECT id, reason, risk_label FROM commands ORDER BY position"
        ).fetchall()
    # Every reason and risk_label that existed on the v2 DB must survive.
    assert [(r["id"], r["reason"], r["risk_label"]) for r in rows] == [
        ("c1", "cleanup before deploy", "recursive force delete"),
        ("c2", "check identity", None),
    ]


def test_migration_handles_preexisting_commands_new(tmp_path: Path) -> None:
    """Crash-safety follow-up: if a previous failed migration left a
    ``commands_new`` table behind (between DROP and RENAME), the next
    ``init_database`` must DROP it before recreating. Without this, the
    second ``CREATE TABLE commands_new`` would fail with
    "table commands_new already exists" and the migration would be
    blocked forever.
    """
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as raw:
        _ = raw.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                requested_by_agent TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                position INTEGER NOT NULL,
                server_alias TEXT NOT NULL,
                server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
                command TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'executed', 'failed')
                ),
                result TEXT,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE (batch_id, position)
            );
            INSERT INTO batches
                (id, title, created_at)
            VALUES
                ('b1', 'lot', '2026-01-01T00:00:00Z');
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type, command, status, created_at)
            VALUES
                ('c1', 'b1', 0, 'srv', 'linux', 'uptime', 'pending', '2026-01-01T00:00:00Z');
            INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()

    # Simulate a crashed previous migration: a leftover commands_new table
    # that would block the migration if we didn't DROP IF EXISTS first.
    with sqlite3.connect(db_path) as raw:
        _ = raw.execute(
            "CREATE TABLE commands_new (id TEXT PRIMARY KEY, leftover INTEGER)"
        )
        _ = raw.execute("INSERT INTO commands_new VALUES ('orphan', 1)")
        raw.commit()

    init_database(Database(path=db_path))

    with sqlite3.connect(db_path) as raw:
        leftover = raw.execute(
            "SELECT name FROM sqlite_master WHERE name = 'commands_new'"
        ).fetchall()
    assert leftover == [], (
        "commands_new should have been dropped or renamed to commands"
    )
    with sqlite3.connect(db_path) as raw:
        ids = [r[0] for r in raw.execute("SELECT id FROM commands ORDER BY position")]
    assert ids == ["c1"]


def test_migration_wraps_recreation_in_explicit_transaction(tmp_path: Path) -> None:
    """The migration script starts with ``BEGIN IMMEDIATE`` and ends with
    ``COMMIT`` -- otherwise ``conn.executescript`` auto-commits each
    statement and a crash between them leaves a half-migrated DB.

    We verify this two ways: by reading the source code of
    ``_widen_commands_status_check_if_needed`` (so a future refactor that
    drops the transaction boundaries is caught at review time) and by
    running the migration on a v2 DB and confirming the original
    command row survives -- if the migration lost the BEGIN, the
    table swap would happen before the INSERT and a crash mid-way
    would lose the data.
    """
    # 1. Source-level check: BEGIN IMMEDIATE and COMMIT must both appear.
    import inspect

    from cgate.db import connection as db_connection

    source = inspect.getsource(db_connection._widen_commands_status_check_if_needed)
    assert "BEGIN IMMEDIATE" in source, (
        "the migration lost its BEGIN IMMEDIATE -- without an explicit "
        "transaction, executescript auto-commits each statement and a crash "
        "between DROP and RENAME orphans every row in commands_new."
    )
    assert "COMMIT;" in source, (
        "the migration lost its COMMIT -- the BEGIN block stays open "
        "until the connection closes, blocking the rest of init_database."
    )
    assert "DROP TABLE IF EXISTS commands_new" in source, (
        "the migration lost its DROP TABLE IF EXISTS commands_new -- a "
        "previous crashed migration would leave commands_new behind and "
        "block the next migration with 'table already exists'."
    )

    # 2. End-to-end check: run the migration on a v2 DB and confirm the
    # original data survives. (A missing BEGIN would not break this test
    # because there is no crash mid-way -- but the source check above
    # already proves the BEGIN is present. The end-to-end is a smoke
    # test against the data path itself.)
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as raw:
        _ = raw.executescript(
            """
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                requested_by_agent TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                position INTEGER NOT NULL,
                server_alias TEXT NOT NULL,
                server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
                command TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'executed', 'failed')
                ),
                result TEXT,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE (batch_id, position)
            );
            INSERT INTO batches
                (id, title, created_at)
            VALUES
                ('b1', 'lot', '2026-01-01T00:00:00Z');
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type, command, status, created_at)
            VALUES
                ('c1', 'b1', 0, 'srv', 'linux', 'uptime', 'pending', '2026-01-01T00:00:00Z');
            INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()

    init_database(Database(path=db_path))

    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'commands'"
        ).fetchone()[0]
        ids = [r[0] for r in raw.execute("SELECT id FROM commands ORDER BY position")]
    assert "'executing'" in sql, "the migration did not run"
    assert ids == ["c1"], "the original command row was lost"


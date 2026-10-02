"""Database layer tests: schema application, repository CRUD, FIFO ordering, constraints."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo, all_terminal
from cgate.db.connection import Database, connect, init_database
from cgate.db.mode import AppModeNotSetError, AppModeRepo, Mode
from cgate.db.schema import SCHEMA_VERSION
from cgate.db.server_settings import ServerSettingsRepo
from cgate.db.types import BatchId, CommandStatus, ServerType

if TYPE_CHECKING:
    from pathlib import Path


def _db(tmp_path: Path, name: str = "cgate.db") -> Database:
    """Fresh isolated DB per test."""
    db = Database(path=tmp_path / name)
    init_database(db)
    return db


def test_init_creates_required_tables(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with connect(db) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"batches", "commands", "schema_version"}.issubset(tables)


def test_init_records_schema_version(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with connect(db) as conn:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
    assert row is not None
    assert row["version"] == SCHEMA_VERSION


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_database(db)  # second call must not raise
    with connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_version"
        ).fetchone()
    assert count["n"] == 1


def test_create_and_get_batch_roundtrip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    created = repo.create(
        title="Restart svc X",
        description="After 3pm deploy",
        requested_by_agent="claude-code",
    )
    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.title == "Restart svc X"
    assert fetched.description == "After 3pm deploy"
    assert fetched.requested_by_agent == "claude-code"
    assert fetched.resolved_at is None


def test_get_missing_batch_returns_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    assert repo.get(BatchId("does-not-exist")) is None


def test_create_batch_with_optional_nulls(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    created = repo.create(
        title="minimal",
        description=None,
        requested_by_agent=None,
    )
    fetched = repo.get(created.id)
    assert fetched is not None
    assert fetched.description is None
    assert fetched.requested_by_agent is None


def test_list_pending_is_fifo_by_created_at(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Insert via raw SQL with controlled timestamps so the test is
    # deterministic regardless of clock resolution / insertion rate.
    rows_in = (
        ("b1", "A", "2025-01-01T00:00:00Z"),
        ("b2", "B", "2025-01-01T00:00:01Z"),
        ("b3", "C", "2025-01-01T00:00:02Z"),
    )
    with connect(db) as conn:
        for batch_id, title, created_at in rows_in:
            _ = conn.execute(
                "INSERT INTO batches (id, title, created_at) VALUES (?, ?, ?)",
                (batch_id, title, created_at),
            )
    repo = BatchesRepo(db)
    pending_titles = tuple(b.title for b in repo.list_pending())
    expected_titles = tuple(title for _bid, title, _ts in rows_in)
    assert pending_titles == expected_titles


def test_mark_resolved_removes_batch_from_pending(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    a = repo.create(title="A", description=None, requested_by_agent=None)
    repo.create(title="B", description=None, requested_by_agent=None)
    repo.mark_resolved(a.id)
    remaining_titles = [batch.title for batch in repo.list_pending()]
    assert remaining_titles == ["B"]
    assert repo.get(a.id) is not None
    assert repo.get(a.id).resolved_at is not None  # type: ignore[union-attr]


def test_mark_resolved_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    a = repo.create(title="A", description=None, requested_by_agent=None)
    repo.mark_resolved(a.id)
    repo.mark_resolved(a.id)
    fetched = repo.get(a.id)
    assert fetched is not None
    assert fetched.resolved_at is not None


def test_list_resolved_returns_only_resolved_most_recent_first(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    older = repo.create(title="older", description=None, requested_by_agent=None)
    newer = repo.create(title="newer", description=None, requested_by_agent=None)
    still_open = repo.create(title="open", description=None, requested_by_agent=None)
    repo.mark_resolved(older.id, resolved_at=datetime(2025, 1, 1, tzinfo=UTC))
    repo.mark_resolved(newer.id, resolved_at=datetime(2025, 1, 2, tzinfo=UTC))

    resolved = repo.list_resolved()

    assert [b.id for b in resolved] == [newer.id, older.id]
    assert still_open.id not in [b.id for b in resolved]


def test_list_resolved_respects_limit(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    total_batches = 5
    query_limit = 2
    for i in range(total_batches):
        batch = repo.create(title=f"b{i}", description=None, requested_by_agent=None)
        repo.mark_resolved(batch.id, resolved_at=datetime(2025, 1, 1 + i, tzinfo=UTC))

    assert len(repo.list_resolved(limit=query_limit)) == query_limit


def test_list_resolved_limit_none_returns_everything(tmp_path: Path) -> None:
    """`cgate history export` wants the full audit trail by default, unlike
    the History screen's bounded browse list -- SQLite's own `LIMIT -1`
    convention makes "unlimited" a one-line special case, not a second
    query shape."""
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    total_batches = 5
    for i in range(total_batches):
        batch = repo.create(title=f"b{i}", description=None, requested_by_agent=None)
        repo.mark_resolved(batch.id, resolved_at=datetime(2025, 1, 1 + i, tzinfo=UTC))

    assert len(repo.list_resolved(limit=None)) == total_batches


def test_list_resolved_empty_when_nothing_resolved_yet(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = BatchesRepo(db)
    repo.create(title="open", description=None, requested_by_agent=None)

    assert repo.list_resolved() == []


def test_add_command_persists_reason(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.LINUX,
        command="uptime",
        reason="checking for a memory leak",
    )
    assert cmd.reason == "checking for a memory leak"
    fetched = commands.get(cmd.id)
    assert fetched is not None
    assert fetched.reason == "checking for a memory leak"


def test_add_command_reason_defaults_to_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.LINUX,
        command="uptime",
    )
    assert cmd.reason is None


def test_add_command_auto_increments_position_from_zero(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    c0 = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.WINDOWS,
        command="Get-Service",
    )
    c1 = commands.add(
        batch_id=batch.id,
        server_alias="srv-2",
        server_type=ServerType.LINUX,
        command="systemctl status nginx",
    )
    c2 = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.WINDOWS,
        command="Restart-Service",
    )
    assert (c0.position, c1.position, c2.position) == (0, 1, 2)
    fetched = commands.list_for_batch(batch.id)
    assert [c.position for c in fetched] == [0, 1, 2]
    assert [c.server_type for c in fetched] == [
        ServerType.WINDOWS,
        ServerType.LINUX,
        ServerType.WINDOWS,
    ]


def test_command_starts_in_pending_state(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.LINUX,
        command="uptime",
    )
    assert cmd.status == CommandStatus.PENDING
    assert cmd.resolved_at is None
    assert cmd.approved_by is None
    assert cmd.result is None


def test_update_status_rejected_records_approved_by_and_resolved_at(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.WINDOWS,
        command="Get-Service",
    )
    commands.update_status(
        cmd.id,
        status=CommandStatus.REJECTED,
        approved_by="wlopez",
    )
    fetched = commands.get(cmd.id)
    assert fetched is not None
    assert fetched.status == CommandStatus.REJECTED
    assert fetched.approved_by == "wlopez"
    assert fetched.resolved_at is not None


def test_update_status_executed_records_result(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.LINUX,
        command="uptime",
    )
    commands.update_status(
        cmd.id,
        status=CommandStatus.EXECUTED,
        approved_by="wlopez",
        result=" 14:32 up 3 days,  2:15, 1 user",
    )
    fetched = commands.get(cmd.id)
    assert fetched is not None
    assert fetched.status == CommandStatus.EXECUTED
    assert fetched.result == " 14:32 up 3 days,  2:15, 1 user"
    assert fetched.resolved_at is not None


def test_update_status_approved_does_not_set_resolved_at(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.LINUX,
        command="true",
    )
    commands.update_status(cmd.id, status=CommandStatus.APPROVED)
    fetched = commands.get(cmd.id)
    assert fetched is not None
    assert fetched.status == CommandStatus.APPROVED
    assert fetched.resolved_at is None


def test_all_terminal_false_when_any_command_still_approved(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    executed = commands.add(
        batch_id=batch.id, server_alias="srv-1", server_type=ServerType.LINUX, command="a"
    )
    approved = commands.add(
        batch_id=batch.id, server_alias="srv-1", server_type=ServerType.LINUX, command="b"
    )
    commands.update_status(executed.id, status=CommandStatus.EXECUTED)
    commands.update_status(approved.id, status=CommandStatus.APPROVED)

    assert all_terminal(commands.list_for_batch(batch.id)) is False


def test_all_terminal_true_when_every_command_is_terminal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    executed = commands.add(
        batch_id=batch.id, server_alias="srv-1", server_type=ServerType.LINUX, command="a"
    )
    rejected = commands.add(
        batch_id=batch.id, server_alias="srv-1", server_type=ServerType.LINUX, command="b"
    )
    commands.update_status(executed.id, status=CommandStatus.EXECUTED)
    commands.update_status(rejected.id, status=CommandStatus.REJECTED)

    assert all_terminal(commands.list_for_batch(batch.id)) is True


def test_list_by_status_returns_only_matching_commands_across_batches(tmp_path: Path) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    lot_a = batches.create(title="a", description=None, requested_by_agent=None)
    lot_b = batches.create(title="b", description=None, requested_by_agent=None)
    approved = commands.add(
        batch_id=lot_a.id, server_alias="srv-1", server_type=ServerType.LINUX, command="a"
    )
    pending = commands.add(
        batch_id=lot_b.id, server_alias="srv-1", server_type=ServerType.LINUX, command="b"
    )
    commands.update_status(approved.id, status=CommandStatus.APPROVED)

    result = commands.list_by_status(CommandStatus.APPROVED)

    assert [c.id for c in result] == [approved.id]
    assert pending.id not in [c.id for c in result]


def test_update_status_expected_status_guard_blocks_stale_transition(
    tmp_path: Path,
) -> None:
    """issue #13: a CAS guard must refuse to apply when the row has already
    moved on, and report that nothing changed."""
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.LINUX,
        command="true",
    )
    # Someone else already rejected it.
    _ = commands.update_status(cmd.id, status=CommandStatus.REJECTED)

    applied = commands.update_status(
        cmd.id,
        status=CommandStatus.APPROVED,
        expected_status=CommandStatus.PENDING,
    )

    assert applied is False
    fetched = commands.get(cmd.id)
    assert fetched is not None
    assert fetched.status == CommandStatus.REJECTED


def test_update_status_expected_status_guard_allows_matching_transition(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    batch = batches.create(title="b", description=None, requested_by_agent=None)
    cmd = commands.add(
        batch_id=batch.id,
        server_alias="srv-1",
        server_type=ServerType.LINUX,
        command="true",
    )

    applied = commands.update_status(
        cmd.id,
        status=CommandStatus.APPROVED,
        expected_status=CommandStatus.PENDING,
    )

    assert applied is True
    fetched = commands.get(cmd.id)
    assert fetched is not None
    assert fetched.status == CommandStatus.APPROVED


def test_check_constraint_rejects_unknown_status(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Setup: parent batch must exist for the FK.
    with connect(db) as conn:
        _ = conn.execute(
            "INSERT INTO batches (id, title, created_at) VALUES (?, ?, ?)",
            ("b1", "T", "2025-01-01T00:00:00Z"),
        )
    # Action: insert with bogus status must raise (single statement under raises).
    with connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        _ = conn.execute(
            """
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type,
                 command, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c1", "b1", 0, "alias", "windows", "cmd", "bogus", "2025-01-01T00:00:00Z"),
        )


def test_check_constraint_rejects_unknown_server_type(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with connect(db) as conn:
        _ = conn.execute(
            "INSERT INTO batches (id, title, created_at) VALUES (?, ?, ?)",
            ("b1", "T", "2025-01-01T00:00:00Z"),
        )
    with connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        _ = conn.execute(
            """
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type,
                 command, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c1", "b1", 0, "alias", "freebsd", "cmd", "pending", "2025-01-01T00:00:00Z"),
        )


def test_unique_constraint_on_batch_id_and_position(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Setup: parent batch + first command.
    with connect(db) as conn:
        _ = conn.execute(
            "INSERT INTO batches (id, title, created_at) VALUES (?, ?, ?)",
            ("b1", "T", "2025-01-01T00:00:00Z"),
        )
        _ = conn.execute(
            """
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type,
                 command, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c1", "b1", 0, "a", "windows", "x", "pending", "2025-01-01T00:00:00Z"),
        )
    # Action: another command with same (batch_id, position) — must raise.
    with connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        _ = conn.execute(
            """
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type,
                 command, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c2", "b1", 0, "a", "linux", "y", "pending", "2025-01-01T00:00:02Z"),
        )


def test_foreign_key_rejects_orphan_command(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        _ = conn.execute(
            """
            INSERT INTO commands
                (id, batch_id, position, server_alias, server_type,
                 command, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c-orphan", "no-batch", 0, "a", "windows", "x", "pending", "2025-01-01T00:00:00Z"),
        )


def test_dbs_are_isolated_per_path(tmp_path: Path) -> None:
    db1 = _db(tmp_path, name="a.db")
    db2 = _db(tmp_path, name="b.db")
    repo1 = BatchesRepo(db1)
    repo2 = BatchesRepo(db2)
    repo1.create(title="a1", description=None, requested_by_agent=None)
    assert len(repo1.list_pending()) == 1
    assert len(repo2.list_pending()) == 0


def test_init_database_enables_wal_journal_mode(tmp_path: Path) -> None:
    """issue #42: `init_database` must set `PRAGMA journal_mode=WAL` so
    readers do not block writers -- the cgate watch TUI polls every 1.5s
    while an MCP server process writes concurrently, and the docstring on
    this module already calls that the documented dual-process design.

    The pragma lives on its own connection (see ``_enable_wal_mode``) so
    that the DDL/DML sequence in the main ``connect`` context does not
    leave Python's sqlite3 module in a state where the final commit
    raises "cannot commit transaction - SQL statements in progress".
    """
    db = _db(tmp_path)
    with connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_wal_mode_persists_across_connections(tmp_path: Path) -> None:
    """WAL is sticky on the database file itself, not just the connection.
    A subsequent `connect` call (or one opened by a different process) must
    observe WAL without re-applying the pragma. Verifying here so a future
    refactor that moves the pragma out of `init_database` cannot regress
    this property without a test failure.
    """
    db = _db(tmp_path)
    # Second connection, fresh.
    with connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_wal_mode_silently_upgrades_an_existing_rollback_journal_db(
    tmp_path: Path,
) -> None:
    """Existing databases that were created before this change are using
    the rollback journal. The first ``init_database`` call against them
    must silently upgrade them to WAL -- no schema_version bump, no data
    migration, no error. The pragma's return value confirms it took.

    Setup: create the DB file directly via raw sqlite3 (which never sets
    WAL), sanity-check that the file is in the SQLite default
    ``delete`` (rollback) journal mode, then run ``init_database`` and
    verify the upgrade happened.
    """
    db_path = tmp_path / "legacy.db"
    db = Database(path=db_path)
    # Touch the file via raw sqlite3, without going through
    # ``init_database`` (so the WAL pragma is never applied).
    with sqlite3.connect(db_path) as raw_conn:
        raw_conn.row_factory = sqlite3.Row
        initial = raw_conn.execute("PRAGMA journal_mode").fetchone()[0]
    # Sanity: a freshly-created DB without our PRAGMA should still be in
    # rollback journal mode (the SQLite default).
    assert initial.lower() == "delete", (
        "Sanity: a freshly-created DB without our PRAGMA should still be in "
        "rollback journal mode (the SQLite default)."
    )
    # Now go through cgate's init_database and verify WAL is enabled.
    init_database(db)
    with connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_wal_mode_enabling_is_idempotent_across_repeated_init(tmp_path: Path) -> None:
    """``init_database`` may be called more than once on the same database
    (idempotency is part of its contract). The WAL pragma must continue
    to report "wal" -- never silently flip to "delete" or another mode --
    after the second call.
    """
    db = _db(tmp_path)
    init_database(db)
    init_database(db)
    with connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_concurrent_reader_does_not_block_under_wal(tmp_path: Path) -> None:
    """The whole point of WAL for cgate: a reader holding an open
    transaction must not block a writer from acquiring the reserved lock.
    Without WAL the writer would block until the reader's transaction
    ended (or, with the 30s busy-timeout, return "database is locked"
    spuriously in the TUI). Under WAL a writer succeeds immediately.
    """
    db = _db(tmp_path)
    with connect(db) as reader:
        # Open a read transaction explicitly so the writer has to wait
        # for it under rollback-journal mode.
        _ = reader.execute("BEGIN")
        _ = reader.execute("SELECT 1").fetchone()
        # A second connection writes -- under WAL this succeeds without
        # touching the reader's transaction.
        with connect(db) as writer:
            _ = writer.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (999_999, "2026-10-02T00:00:00Z"),
            )
        # The reader's transaction is still valid; we can still see the
        # pre-write snapshot.
        _ = reader.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
        _ = reader.execute("ROLLBACK")


def test_init_creates_app_mode_and_server_settings_tables(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with connect(db) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"app_mode", "server_settings"}.issubset(tables)


def test_init_twice_with_mode_tables_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_database(db)
    init_database(db)
    with connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_version"
        ).fetchone()
    assert count["n"] == 1


def test_app_mode_get_raises_when_unset(tmp_path: Path) -> None:
    repo = AppModeRepo(_db(tmp_path))
    with pytest.raises(AppModeNotSetError):
        _ = repo.get()


def test_app_mode_set_then_get_roundtrip(tmp_path: Path) -> None:
    repo = AppModeRepo(_db(tmp_path))
    created = repo.set(mode=Mode.AUTO, updated_by="wlopez")
    assert created.mode is Mode.AUTO
    assert created.updated_by == "wlopez"
    fetched = repo.get()
    assert fetched.mode is Mode.AUTO
    assert fetched.updated_by == "wlopez"
    assert fetched.updated_at == created.updated_at


def test_app_mode_set_upserts_single_row(tmp_path: Path) -> None:
    repo = AppModeRepo(_db(tmp_path))
    _ = repo.set(mode=Mode.PROPOSE, updated_by="a")
    _ = repo.set(mode=Mode.AUTO, updated_by="b")
    fetched = repo.get()
    assert fetched.mode is Mode.AUTO
    assert fetched.updated_by == "b"


def test_server_settings_get_returns_none_when_unset(tmp_path: Path) -> None:
    repo = ServerSettingsRepo(_db(tmp_path))
    assert repo.get("srv") is None


def test_server_settings_get_or_default_returns_not_allowed_when_unset(
    tmp_path: Path,
) -> None:
    repo = ServerSettingsRepo(_db(tmp_path))
    setting = repo.get_or_default("srv")
    assert setting.server_alias == "srv"
    assert setting.auto_allowed is False
    assert setting.updated_at is None
    assert setting.updated_by is None


def test_server_settings_set_then_get_roundtrip(tmp_path: Path) -> None:
    repo = ServerSettingsRepo(_db(tmp_path))
    created = repo.set(alias="srv", auto_allowed=True, updated_by="wlopez")
    assert created.auto_allowed is True
    assert created.updated_at is not None
    fetched = repo.get("srv")
    assert fetched is not None
    assert fetched.auto_allowed is True
    assert fetched.updated_by == "wlopez"
    assert fetched.updated_at == created.updated_at


def test_server_settings_set_upserts_existing_alias(tmp_path: Path) -> None:
    repo = ServerSettingsRepo(_db(tmp_path))
    _ = repo.set(alias="srv", auto_allowed=True, updated_by="a")
    _ = repo.set(alias="srv", auto_allowed=False, updated_by="b")
    fetched = repo.get("srv")
    assert fetched is not None
    assert fetched.auto_allowed is False
    assert fetched.updated_by == "b"


def test_server_settings_list_all_returns_only_explicit_rows(tmp_path: Path) -> None:
    repo = ServerSettingsRepo(_db(tmp_path))
    _ = repo.set(alias="beta", auto_allowed=True, updated_by="a")
    _ = repo.set(alias="alpha", auto_allowed=False, updated_by="a")
    _ = repo.get_or_default("ghost")
    assert [setting.server_alias for setting in repo.list_all()] == ["alpha", "beta"]

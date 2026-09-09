"""Database layer tests: schema application, repository CRUD, FIFO ordering, constraints."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, connect, init_database
from cgate.db.schema import SCHEMA_VERSION
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

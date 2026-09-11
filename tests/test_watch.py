from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cgate.cli.main import app
from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, connect, init_database
from cgate.db.types import Batch, BatchId, Command, CommandStatus, ServerType
from cgate.executor.base import ErrorKind, ExecutionResult
from cgate.watch.approval import approve_one, approve_remaining, reject_one, reject_remaining
from cgate.watch.queue import (
    active_batch,
    count_waiting,
    is_batch_resolved,
    pending_commands_in_batch,
)

if TYPE_CHECKING:
    from pathlib import Path

WAITING_BATCHES = 2
COMMAND_COUNT = 3


@dataclass(frozen=True, slots=True)
class Repos:
    db: Database
    batches: BatchesRepo
    commands: CommandsRepo
    connections: ConnectionsRepo


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    return Repos(
        db=db,
        batches=BatchesRepo(db),
        commands=CommandsRepo(db),
        connections=ConnectionsRepo(db),
    )


def _batch(repos: Repos, title: str = "lot") -> Batch:
    return repos.batches.create(
        title=title,
        description="description",
        requested_by_agent="test-agent",
    )


def _command(
    repos: Repos,
    *,
    batch_id: BatchId | None = None,
    command: str = "uptime",
) -> Command:
    lot = _batch(repos) if batch_id is None else None
    resolved_batch_id = lot.id if lot is not None else batch_id
    return repos.commands.add(
        batch_id=resolved_batch_id,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command=command,
    )


def _add_connection(repos: Repos) -> None:
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )


def _success() -> ExecutionResult:
    return ExecutionResult("hello\n", "", 0, 42, None)


def test_active_batch_returns_oldest_pending(repos: Repos) -> None:
    """Insert via raw SQL with controlled timestamps so FIFO is deterministic
    regardless of clock resolution / insertion rate.
    """
    rows_in = (
        ("b1", "first", "2025-01-01T00:00:00Z"),
        ("b2", "second", "2025-01-01T00:00:01Z"),
        ("b3", "third", "2025-01-01T00:00:02Z"),
    )
    with connect(repos.db) as conn:
        for batch_id, title, created_at in rows_in:
            _ = conn.execute(
                "INSERT INTO batches (id, title, created_at) VALUES (?, ?, ?)",
                (batch_id, title, created_at),
            )
    active = active_batch(repos.batches)
    assert active is not None
    assert active.id == "b1"
    assert active.title == "first"


def test_active_batch_returns_none_when_no_pending(repos: Repos) -> None:
    assert active_batch(repos.batches) is None


def test_count_waiting_returns_zero_when_no_lots(repos: Repos) -> None:
    assert count_waiting(repos.batches) == 0


def test_count_waiting_excludes_active_one(repos: Repos) -> None:
    for title in ("first", "second", "third"):
        _ = _batch(repos, title)

    assert count_waiting(repos.batches) == WAITING_BATCHES


def test_pending_commands_in_batch_filters_terminal(repos: Repos) -> None:
    lot = _batch(repos)
    commands = [_command(repos, batch_id=lot.id, command=str(index)) for index in range(5)]
    for cmd, status in zip(commands[1:], list(CommandStatus)[1:], strict=True):
        repos.commands.update_status(cmd.id, status=status)

    pending = pending_commands_in_batch(repos.commands.list_for_batch(lot.id))

    assert pending == [commands[0]]


def test_is_batch_resolved_returns_false_when_any_pending(repos: Repos) -> None:
    lot = _batch(repos)
    pending = _command(repos, batch_id=lot.id)
    rejected = _command(repos, batch_id=lot.id)
    repos.commands.update_status(rejected.id, status=CommandStatus.REJECTED)

    assert is_batch_resolved([pending, repos.commands.get(rejected.id)]) is False


def test_is_batch_resolved_returns_true_when_all_terminal(repos: Repos) -> None:
    lot = _batch(repos)
    commands = [
        _command(repos, batch_id=lot.id, command=str(index))
        for index in range(COMMAND_COUNT)
    ]
    for cmd, status in zip(
        commands,
        (CommandStatus.EXECUTED, CommandStatus.REJECTED, CommandStatus.FAILED),
        strict=True,
    ):
        repos.commands.update_status(cmd.id, status=status)

    refreshed = repos.commands.list_for_batch(lot.id)
    assert is_batch_resolved(refreshed) is True


def test_approve_one_marks_executed_and_calls_executor(repos: Repos) -> None:
    _add_connection(repos)
    cmd = _command(repos)

    with patch("cgate.watch.approval.execute_command", return_value=_success()) as execute:
        updated, result = approve_one(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    assert updated.status is CommandStatus.EXECUTED
    assert updated.approved_by is not None
    assert result == _success()
    execute.assert_called_once()


def test_approve_one_marks_failed_when_executor_returns_error(repos: Repos) -> None:
    _add_connection(repos)
    cmd = _command(repos)
    failed = ExecutionResult("", "denied", -1, 9, ErrorKind.AUTH_FAILED)

    with patch("cgate.watch.approval.execute_command", return_value=failed):
        updated, result = approve_one(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    assert updated.status is CommandStatus.FAILED
    assert updated.result == "--- stderr ---\ndenied"
    assert result is failed


def test_approve_one_does_not_execute_when_it_loses_the_race(repos: Repos) -> None:
    """issue #13: if update_status's CAS guard reports the row already
    moved on -- a race between the initial PENDING check and the write --
    approve_one must not call the executor."""
    _add_connection(repos)
    cmd = _command(repos)

    with (
        patch("cgate.watch.approval.execute_command") as execute,
        patch.object(repos.commands, "update_status", return_value=False) as update_status,
    ):
        updated, result = approve_one(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    update_status.assert_called_once()
    execute.assert_not_called()
    assert result is None
    assert updated is not None
    assert updated.status is CommandStatus.PENDING


def test_reject_one_marks_rejected_and_does_not_execute(repos: Repos) -> None:
    cmd = _command(repos)

    with patch("cgate.watch.approval.execute_command") as execute:
        updated = reject_one(
            commands=repos.commands,
            batches=repos.batches,
            command_id=cmd.id,
        )

    assert updated.status is CommandStatus.REJECTED
    assert updated.approved_by is not None
    execute.assert_not_called()


def test_approve_remaining_processes_all_pending(repos: Repos) -> None:
    _add_connection(repos)
    lot = _batch(repos)
    commands = [
        _command(repos, batch_id=lot.id, command=str(index))
        for index in range(COMMAND_COUNT)
    ]

    with patch("cgate.watch.approval.execute_command", return_value=_success()) as execute:
        results = approve_remaining(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            remaining=commands,
        )

    assert [command.status for command, _result in results] == [
        CommandStatus.EXECUTED
    ] * COMMAND_COUNT
    assert execute.call_count == COMMAND_COUNT
    assert repos.batches.get(lot.id).resolved_at is not None


def test_approve_remaining_records_connect_failure_and_continues(repos: Repos) -> None:
    lot = _batch(repos)
    commands = [_command(repos, batch_id=lot.id, command=str(index)) for index in range(2)]

    results = approve_remaining(
        db=repos.db,
        commands=repos.commands,
        connections=repos.connections,
        batches=repos.batches,
        remaining=commands,
    )

    assert [command.status for command, _result in results] == [CommandStatus.FAILED] * 2
    assert all("connection 'linux-1' not found" in (cmd.result or "") for cmd, _ in results)


def test_reject_remaining_processes_all_pending_without_executor(repos: Repos) -> None:
    lot = _batch(repos)
    commands = [
        _command(repos, batch_id=lot.id, command=str(index))
        for index in range(COMMAND_COUNT)
    ]

    with patch("cgate.watch.approval.execute_command") as execute:
        results = reject_remaining(
            commands=repos.commands,
            batches=repos.batches,
            remaining=commands,
        )

    assert [command.status for command in results] == [
        CommandStatus.REJECTED
    ] * COMMAND_COUNT
    assert repos.batches.get(lot.id).resolved_at is not None
    execute.assert_not_called()


def test_batch_resolves_only_after_last_command_is_terminal(repos: Repos) -> None:
    lot = _batch(repos)
    first = _command(repos, batch_id=lot.id, command="first")
    second = _command(repos, batch_id=lot.id, command="second")

    _ = reject_one(commands=repos.commands, batches=repos.batches, command_id=first.id)
    assert repos.batches.get(lot.id).resolved_at is None

    _ = reject_one(commands=repos.commands, batches=repos.batches, command_id=second.id)
    assert repos.batches.get(lot.id).resolved_at is not None


def test_cli_watch_prints_no_pending_when_db_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CGATE_DB_PATH", str(tmp_path / "cgate.db"))

    result = CliRunner().invoke(app, ["watch"])

    assert result.exit_code == 0
    assert "No pending batches" in result.output

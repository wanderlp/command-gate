from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, connect, init_database
from cgate.db.types import Batch, BatchId, Command, CommandStatus, ServerType
from cgate.executor.base import ErrorKind, ExecutionResult
from cgate.watch.approval import (
    ConnectionNotFoundError,
    approve_one,
    approve_remaining,
    execute_and_finalize,
    mark_approved,
    reject_one,
    reject_remaining,
)
from cgate.watch.queue import (
    active_batch,
    count_pending_commands,
    count_waiting,
    fail_orphaned_approvals,
    heal_queue,
    is_batch_resolved,
    pending_commands_in_batch,
    resolve_stale_batches,
    select_active_batch,
)
from cgate.watch.session import run_watch_session

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


def _batch_value(batch_id: str, title: str) -> Batch:
    """A plain Batch value, unpersisted -- for pure functions that only read fields."""
    return Batch(
        id=BatchId(batch_id),
        title=title,
        description=None,
        requested_by_agent=None,
        created_at=datetime.now(UTC),
        resolved_at=None,
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


def test_select_active_batch_returns_fifo_oldest_when_nothing_pinned() -> None:
    older = _batch_value("b1", "first")
    newer = _batch_value("b2", "second")

    assert select_active_batch([older, newer], None) is older


def test_select_active_batch_returns_pinned_batch_even_if_not_oldest() -> None:
    """A human can jump the queue and prioritize a batch further down --
    the whole point of adding batch selection to cgate watch."""
    older = _batch_value("b1", "first")
    newer = _batch_value("b2", "second")

    assert select_active_batch([older, newer], BatchId("b2")) is newer


def test_select_active_batch_falls_back_to_fifo_once_pinned_batch_resolves() -> None:
    """Self-correcting: once the pinned batch is no longer in `pending`
    (it resolved), this must not get stuck returning None forever."""
    remaining = _batch_value("b2", "second")

    assert select_active_batch([remaining], BatchId("b1")) is remaining


def test_select_active_batch_returns_none_when_queue_is_empty() -> None:
    assert select_active_batch([], None) is None


def test_count_pending_commands_sums_across_every_pending_batch(repos: Repos) -> None:
    first_lot = _batch(repos, "first")
    second_lot = _batch(repos, "second")
    commands_in_first_lot = 2
    for _ in range(commands_in_first_lot):
        _command(repos, batch_id=first_lot.id)
    _command(repos, batch_id=second_lot.id)
    expected_total = commands_in_first_lot + 1

    total = count_pending_commands([first_lot, second_lot], repos.commands)

    assert total == expected_total


def test_count_pending_commands_excludes_terminal_commands(repos: Repos) -> None:
    lot = _batch(repos)
    terminal = _command(repos, batch_id=lot.id)
    _ = _command(repos, batch_id=lot.id)
    repos.commands.update_status(terminal.id, status=CommandStatus.REJECTED)

    assert count_pending_commands([lot], repos.commands) == 1


def test_count_waiting_returns_zero_when_no_lots(repos: Repos) -> None:
    assert count_waiting(repos.batches) == 0


def test_count_waiting_excludes_active_one(repos: Repos) -> None:
    for title in ("first", "second", "third"):
        _ = _batch(repos, title)

    assert count_waiting(repos.batches) == WAITING_BATCHES


def test_pending_commands_in_batch_filters_terminal(repos: Repos) -> None:
    """Every terminal status filters out; EXECUTING (non-terminal, #36/#38) does too.

    ``pending_commands_in_batch`` is the human-decision queue --
    PENDING only. Both terminal statuses (REJECTED, EXECUTED, FAILED) and
    transient-but-decided statuses (APPROVED, EXECUTING) must filter out.
    """
    lot = _batch(repos)
    commands = [_command(repos, batch_id=lot.id, command=str(index)) for index in range(5)]
    # One of each terminal + the new EXECUTING state -- the new
    # transient non-PENDING status added by #36/#38 must also be excluded
    # from the human-decision queue.
    statuses = [
        CommandStatus.REJECTED,
        CommandStatus.EXECUTED,
        CommandStatus.FAILED,
        CommandStatus.EXECUTING,
    ]
    for cmd, status in zip(commands[1:], statuses, strict=True):
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


def test_resolve_stale_batches_marks_resolved_when_all_commands_terminal(repos: Repos) -> None:
    """A command can turn terminal outside approve_one/reject_one (MCP AUTO
    mode auto-executes without ever calling either) -- resolve_stale_batches
    is the sweep that catches the batch it leaves unresolved."""
    lot = _batch(repos)
    cmd = _command(repos, batch_id=lot.id)
    repos.commands.update_status(cmd.id, status=CommandStatus.EXECUTED)

    resolve_stale_batches(repos.batches, repos.commands)

    assert repos.batches.get(lot.id).resolved_at is not None


def test_resolve_stale_batches_leaves_batch_open_when_a_command_is_still_pending(
    repos: Repos,
) -> None:
    lot = _batch(repos)
    _ = _command(repos, batch_id=lot.id)

    resolve_stale_batches(repos.batches, repos.commands)

    assert repos.batches.get(lot.id).resolved_at is None


def test_fail_orphaned_approvals_marks_stuck_approved_commands_failed(repos: Repos) -> None:
    """A command left APPROVED means the process that approved it died
    before executing it -- it can never reach EXECUTED/FAILED on its own."""
    cmd = _command(repos)
    repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED, approved_by="tester")

    fail_orphaned_approvals(repos.commands)

    updated = repos.commands.get(cmd.id)
    assert updated.status is CommandStatus.FAILED
    assert "interrupted" in (updated.result or "").lower()


def test_fail_orphaned_approvals_leaves_pending_commands_alone(repos: Repos) -> None:
    cmd = _command(repos)

    fail_orphaned_approvals(repos.commands)

    assert repos.commands.get(cmd.id).status is CommandStatus.PENDING


def test_heal_queue_resolves_batch_after_failing_its_orphaned_command(repos: Repos) -> None:
    lot = _batch(repos)
    cmd = _command(repos, batch_id=lot.id)
    repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED, approved_by="tester")

    heal_queue(repos.batches, repos.commands)

    assert repos.batches.get(lot.id).resolved_at is not None
    assert repos.commands.get(cmd.id).status is CommandStatus.FAILED


def test_mark_approved_transitions_pending_to_approved_without_executing(repos: Repos) -> None:
    """The fast half of approve_one's split: a plain DB write, no network
    call -- lets the TUI refresh and show "approved, running" before the
    slow half instead of going silent for the whole executor timeout."""
    _add_connection(repos)
    cmd = _command(repos)

    with patch("cgate.executor.selector.execute_command") as execute:
        updated = mark_approved(
            commands=repos.commands, connections=repos.connections, command_id=cmd.id
        )

    assert updated is not None
    assert updated.status is CommandStatus.APPROVED
    execute.assert_not_called()


def test_mark_approved_raises_when_connection_missing(repos: Repos) -> None:
    cmd = _command(repos)  # no connection registered for "linux-1"

    with pytest.raises(ConnectionNotFoundError):
        mark_approved(commands=repos.commands, connections=repos.connections, command_id=cmd.id)


def test_execute_and_finalize_runs_the_executor_and_marks_executed(repos: Repos) -> None:
    _add_connection(repos)
    cmd = _command(repos)
    _ = mark_approved(commands=repos.commands, connections=repos.connections, command_id=cmd.id)

    with patch("cgate.executor.selector.execute_command", return_value=_success()) as execute:
        updated, result = execute_and_finalize(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    assert updated is not None
    assert updated.status is CommandStatus.EXECUTED
    assert result == _success()
    execute.assert_called_once()


def test_execute_and_finalize_noops_when_not_approved(repos: Repos) -> None:
    """A command still PENDING (mark_approved was never called, or lost
    a race) must not be executed."""
    _add_connection(repos)
    cmd = _command(repos)

    with patch("cgate.executor.selector.execute_command") as execute:
        updated, result = execute_and_finalize(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    assert updated is not None
    assert updated.status is CommandStatus.PENDING
    assert result is None
    execute.assert_not_called()


def test_approve_one_marks_executed_and_calls_executor(repos: Repos) -> None:
    _add_connection(repos)
    cmd = _command(repos)

    with patch("cgate.executor.selector.execute_command", return_value=_success()) as execute:
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

    with patch("cgate.executor.selector.execute_command", return_value=failed):
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
        patch("cgate.executor.selector.execute_command") as execute,
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

    with patch("cgate.executor.selector.execute_command") as execute:
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

    with patch("cgate.executor.selector.execute_command", return_value=_success()) as execute:
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

    with patch("cgate.executor.selector.execute_command") as execute:
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


def test_run_watch_session_always_launches_the_dashboard(
    repos: Repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-queue fast exit was removed: `cgate watch` always opens
    the dashboard now, and WatchApp's own idle state handles a queue with
    nothing pending yet."""
    launched: dict[str, object] = {}

    class FakeApp:
        def __init__(self, **kwargs: object) -> None:
            launched.update(kwargs)

        def run(self) -> None:
            launched["ran"] = True

    monkeypatch.setattr("cgate.watch.session.WatchApp", FakeApp)

    run_watch_session(repos.db)

    assert launched["ran"] is True
    assert launched["db"] is repos.db


def test_run_watch_session_heals_the_queue_before_launching(
    repos: Repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch stuck open by a crash or a since-fixed bug must never greet
    the user on the next launch -- heal_queue runs before WatchApp does."""
    lot = _batch(repos)
    cmd = _command(repos, batch_id=lot.id)
    repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED, approved_by="tester")

    class FakeApp:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self) -> None:
            pass

    monkeypatch.setattr("cgate.watch.session.WatchApp", FakeApp)

    run_watch_session(repos.db)

    assert repos.commands.get(cmd.id).status is CommandStatus.FAILED
    assert repos.batches.get(lot.id).resolved_at is not None

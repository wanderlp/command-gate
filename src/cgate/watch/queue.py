"""Pure helpers for selecting the active batch, counting waiters, and healing it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cgate.db.commands import all_terminal
from cgate.db.types import CommandStatus

if TYPE_CHECKING:
    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.types import Batch, BatchId, Command


def active_batch(batches: BatchesRepo) -> Batch | None:
    """Return the next pending batch in FIFO order, or None."""
    pending = batches.list_pending()
    return pending[0] if pending else None


def select_active_batch(pending: list[Batch], pinned_id: BatchId | None) -> Batch | None:
    """Return the pinned batch if it's still pending, else the FIFO-oldest one.

    Lets a human jump the queue and prioritize a specific batch instead
    of being forced through strict FIFO order. Self-correcting: once the
    pinned batch resolves (or otherwise drops out of `pending`), this
    falls straight back to FIFO without needing the caller to notice and
    clear the pin.
    """
    if pinned_id is not None:
        for batch in pending:
            if batch.id == pinned_id:
                return batch
    return pending[0] if pending else None


def count_waiting(batches: BatchesRepo) -> int:
    """Count batches queued behind the active one."""
    return max(0, len(batches.list_pending()) - 1)


def count_pending_commands(pending_batches: list[Batch], commands: CommandsRepo) -> int:
    """Total PENDING commands across every batch still in the queue."""
    return sum(
        len(pending_commands_in_batch(commands.list_for_batch(batch.id)))
        for batch in pending_batches
    )


def pending_commands_in_batch(commands_for_batch: list[Command]) -> list[Command]:
    """Filter pending commands while preserving their repository order."""
    return [
        command
        for command in commands_for_batch
        if command.status is CommandStatus.PENDING
    ]


def is_batch_resolved(commands_for_batch: list[Command]) -> bool:
    """Return whether every command in the batch has a terminal status."""
    return all_terminal(commands_for_batch)


def resolve_stale_batches(batches: BatchesRepo, commands: CommandsRepo) -> None:
    """Stamp `resolved_at` on any pending batch whose commands are all terminal.

    A command can turn terminal without going through the TUI's own
    approve/reject path -- the MCP AUTO-mode auto-execution path is one
    example. Whenever that happens the batch's `resolved_at` must be
    stamped too, or it sits at the head of the FIFO queue forever with
    nothing left to approve or reject, silently blocking every batch
    behind it. This sweeps up any batch left in that state.
    """
    for batch in batches.list_pending():
        commands_for_batch = commands.list_for_batch(batch.id)
        if commands_for_batch and is_batch_resolved(commands_for_batch):
            batches.mark_resolved(batch.id)


def fail_orphaned_approvals(
    commands: CommandsRepo,
    *,
    stuck_threshold_seconds: int = 120,
    executor_timeout_seconds: float = 60.0,
) -> None:
    """Mark commands orphaned by a crashed executor.

    Two flavors of orphan, distinguishable by the executor's CAS claim
    (issue #38):

    - ``APPROVED``: the process died between ``mark_approved`` and the
      CAS to ``EXECUTING`` -- the executor never started. Always an
      orphan. (Heads-up -- there is a millisecond-scale window between
      ``mark_approved`` and the CAS where the row is ``APPROVED`` but a
      live executor is mid-CAS. ``execute_and_finalize`` would lose the
      CAS for that caller and we would mark FAILED here; the executor's
      command would NOT run twice because the CAS protected it.)

    # ``EXECUTING`` with stale ``claimed_at`` (older than the effective
      threshold): the process claimed the command, ran into the
      network call, and then died. We recover here too so the batch
      does not sit stuck in EXECUTING forever.

    Live ``EXECUTING`` commands -- fresh ``claimed_at`` -- are left alone.
    Without this distinction, ``cgate watch`` starting on a machine that
    is still running an AUTO-mode executor would mark that executor's
    in-flight commands FAILED and clobber their audit trail (issue #38).

    The effective stuck threshold is ``max(stuck_threshold_seconds,
    executor_timeout_seconds + 60)`` -- an operator who configures a
    longer ``timeout=300`` for slow commands gets a stuck threshold of
    360s, so a legitimately slow command does not get marked FAILED just
    because the heal ran at the 120s mark.
    """
    # ``executor_timeout_seconds`` of 0 means "no automatic extension --
    # use ``stuck_threshold_seconds`` as-is" (used by tests to keep the
    # threshold deterministic). Otherwise the heal threshold scales with
    # the executor timeout so a legitimately slow command (e.g. with
    # ``timeout=300``) is not marked FAILED at the heal's marker.
    if executor_timeout_seconds > 0:
        effective_threshold = max(
            stuck_threshold_seconds, int(executor_timeout_seconds) + 60
        )
    else:
        effective_threshold = stuck_threshold_seconds
    now = datetime.now(UTC)
    for command in commands.list_by_status(CommandStatus.APPROVED):
        _ = commands.update_status(
            command.id,
            status=CommandStatus.FAILED,
            approved_by=command.approved_by,
            result="interrupted before completion (cgate restarted mid-execution)",
            expected_status=CommandStatus.APPROVED,
        )
    for command in commands.list_by_status(CommandStatus.EXECUTING):
        if command.claimed_at is None:
            # Defensive: should not happen -- the executor CAS stamps
            # claimed_at on the way to EXECUTING. If a manual SQL edit
            # (or a bug in a future change) left this, treat as stuck.
            _ = commands.update_status(
                command.id,
                status=CommandStatus.FAILED,
                approved_by=command.approved_by,
                result=(
                    "interrupted before completion "
                    "(EXECUTING with no claimed_at)"
                ),
                expected_status=CommandStatus.EXECUTING,
            )
            continue
        age = (now - command.claimed_at).total_seconds()
        if age > effective_threshold:
            _ = commands.update_status(
                command.id,
                status=CommandStatus.FAILED,
                approved_by=command.approved_by,
                result=(
                    f"interrupted before completion "
                    f"(claimed_at {age:.0f}s old, threshold "
                    f"{effective_threshold}s)"
                ),
                expected_status=CommandStatus.EXECUTING,
            )


def heal_queue(
    batches: BatchesRepo,
    commands: CommandsRepo,
    *,
    executor_timeout_seconds: float = 60.0,
) -> None:
    """Repair queue state left inconsistent by a crash or a since-fixed bug.

    Runs once when `cgate watch` starts, before the dashboard is shown.
    ``executor_timeout_seconds`` is propagated to ``fail_orphaned_approvals``
    so the heal threshold scales with the configured executor timeout.
    """
    fail_orphaned_approvals(commands, executor_timeout_seconds=executor_timeout_seconds)
    resolve_stale_batches(batches, commands)

"""CommandsRepo: CRUD + status transitions for the `commands` table."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from cgate.db.connection import Database, connect
from cgate.db.rows import col_int, iso, row_to_command
from cgate.db.types import (
    BatchId,
    Command,
    CommandId,
    CommandStatus,
    ServerType,
)

_TERMINAL_STATUSES: frozenset[CommandStatus] = frozenset(
    {CommandStatus.EXECUTED, CommandStatus.REJECTED, CommandStatus.FAILED}
)


def all_terminal(commands_for_batch: list[Command]) -> bool:
    """Return whether every command in the list has reached a terminal status.

    Shared by the watch TUI's own approve/reject path and the MCP AUTO-mode
    auto-execution path, both of which need to decide when a batch is done.
    """
    return all(command.status in _TERMINAL_STATUSES for command in commands_for_batch)


class CommandsRepo:
    """Command CRUD + per-batch listing + status transitions."""

    _db: Database  # class-level annotation required by strict mode

    def __init__(self, db: Database) -> None:
        """Store the database handle for subsequent operations."""
        self._db = db

    def add(  # noqa: PLR0913 - one param per persisted command field, all but the first three optional
        self,
        *,
        batch_id: BatchId,
        server_alias: str,
        server_type: ServerType,
        command: str,
        reason: str | None = None,
        risk_label: str | None = None,
    ) -> Command:
        """Append a command to the end of a batch (position = max + 1, or 0 if empty).

        Computes the position in the same statement as the INSERT (issue
        #10) rather than a separate SELECT beforehand: two connections
        racing the old SELECT-then-INSERT could both compute the same
        ``next_pos`` before either committed, and the second INSERT would
        die on the ``UNIQUE (batch_id, position)`` constraint. A single
        ``INSERT ... SELECT`` is one write that SQLite serializes against
        other writers, so the second racer recomputes against the first
        racer's already-committed row instead of colliding with it.
        """
        command_id = CommandId(str(uuid4()))
        now = datetime.now(UTC)
        with connect(self._db) as conn:
            _ = conn.execute(
                """
                INSERT INTO commands
                    (id, batch_id, position, server_alias, server_type,
                     command, status, created_at, reason, risk_label)
                SELECT ?, ?, COALESCE(MAX(position), -1) + 1, ?, ?, ?, ?, ?, ?, ?
                FROM commands WHERE batch_id = ?
                """,
                (
                    command_id,
                    batch_id,
                    server_alias,
                    server_type.value,
                    command,
                    CommandStatus.PENDING.value,
                    iso(now),
                    reason,
                    risk_label,
                    batch_id,
                ),
            )
            pos_row = conn.execute(
                "SELECT position FROM commands WHERE id = ?", (command_id,)
            ).fetchone()
            if pos_row is None:
                msg = "inserted command disappeared before its position could be read"
                raise RuntimeError(msg)
            next_pos = col_int(pos_row, "position")
        return Command(
            id=command_id,
            batch_id=batch_id,
            position=next_pos,
            server_alias=server_alias,
            server_type=server_type,
            command=command,
            status=CommandStatus.PENDING,
            result=None,
            approved_by=None,
            created_at=now,
            resolved_at=None,
            claimed_at=None,
            reason=reason,
            risk_label=risk_label,
        )

    def get(self, command_id: CommandId) -> Command | None:
        """Fetch a command by ID, or None if not found."""
        with connect(self._db) as conn:
            row = conn.execute(
                """
                SELECT id, batch_id, position, server_alias, server_type,
                    command, status, result, approved_by, created_at, resolved_at,
                    claimed_at, reason, risk_label
                FROM commands WHERE id = ?
                """,
                (command_id,),
            ).fetchone()
        return row_to_command(row) if row is not None else None

    def list_for_batch(self, batch_id: BatchId) -> list[Command]:
        """All commands in a batch, ordered by position."""
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT id, batch_id, position, server_alias, server_type,
                    command, status, result, approved_by, created_at, resolved_at,
                    claimed_at, reason, risk_label
                FROM commands WHERE batch_id = ?
                ORDER BY position ASC
                """,
                (batch_id,),
            ).fetchall()
        return [row_to_command(r) for r in rows]

    def list_by_status(self, status: CommandStatus) -> list[Command]:
        """All commands currently in the given status, across every batch."""
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT id, batch_id, position, server_alias, server_type,
                    command, status, result, approved_by, created_at, resolved_at,
                    claimed_at, reason, risk_label
                FROM commands WHERE status = ?
                """,
                (status.value,),
            ).fetchall()
        return [row_to_command(r) for r in rows]

    def update_status(  # noqa: PLR0913 - signature mirrors the persisted command fields; merging claimed_at onto the existing transaction write is cheaper than splitting it
        self,
        command_id: CommandId,
        *,
        status: CommandStatus,
        approved_by: str | None = None,
        result: str | None = None,
        expected_status: CommandStatus | None = None,
        claimed_at: datetime | None = None,
    ) -> bool:
        """Transition a command's status; stamp resolved_at iff status is terminal.

        When ``expected_status`` is given, the UPDATE only applies if the
        row's current status still matches it -- a compare-and-swap guard
        against a concurrent transition racing this one (issue #13; not
        exploitable in today's single-process synchronous `watch`, but a
        cheap guard against a future daemon/concurrent mode). Returns
        whether the row was actually updated.

        When ``status`` is EXECUTING and ``claimed_at`` is not provided,
        the current time is stamped automatically -- this is the executor
        CAS-claiming the row and the timestamp is what ``fail_orphaned_approvals``
        keys off to tell live processes from dead ones (issue #38).
        Passing ``claimed_at`` explicitly is supported for tests.
        """
        guard = " AND status = ?" if expected_status is not None else ""
        guard_params = (expected_status.value,) if expected_status is not None else ()
        now = datetime.now(UTC)
        if status in _TERMINAL_STATUSES:
            set_clause = "status = ?, approved_by = ?, result = ?, resolved_at = ?"
            params: tuple[object, ...] = (
                status.value,
                approved_by,
                result,
                iso(now),
                command_id,
                *guard_params,
            )
        elif status is CommandStatus.EXECUTING:
            # EXECUTING is non-terminal but DOES need the claimed_at stamp
            # set -- it's the heal's "is this process alive?" signal.
            stamp = iso(claimed_at) if claimed_at is not None else iso(now)
            set_clause = "status = ?, claimed_at = ?, approved_by = ?, result = ?"
            params = (status.value, stamp, approved_by, result, command_id, *guard_params)
        else:
            set_clause = "status = ?, approved_by = ?, result = ?"
            params = (status.value, approved_by, result, command_id, *guard_params)
        with connect(self._db) as conn:
            # set_clause is one of three fixed literals; guard is one of two;
            # neither is ever built from request, function arg, or user input.
            cursor = conn.execute(
                f"UPDATE commands SET {set_clause} WHERE id = ?{guard}",  # noqa: S608
                params,
            )
        return cursor.rowcount > 0

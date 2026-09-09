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


class CommandsRepo:
    """Command CRUD + per-batch listing + status transitions."""

    _db: Database  # class-level annotation required by strict mode

    def __init__(self, db: Database) -> None:
        """Store the database handle for subsequent operations."""
        self._db = db

    def add(
        self,
        *,
        batch_id: BatchId,
        server_alias: str,
        server_type: ServerType,
        command: str,
    ) -> Command:
        """Append a command to the end of a batch (position = max + 1, or 0 if empty)."""
        command_id = CommandId(str(uuid4()))
        now = datetime.now(UTC)
        with connect(self._db) as conn:
            pos_row = conn.execute(
                """
                SELECT COALESCE(MAX(position), -1) + 1 AS next_pos
                FROM commands
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            if pos_row is None:
                msg = "aggregate query returned no row"
                raise RuntimeError(msg)
            next_pos = col_int(pos_row, "next_pos")
            _ = conn.execute(
                """
                INSERT INTO commands
                    (id, batch_id, position, server_alias, server_type,
                     command, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    batch_id,
                    next_pos,
                    server_alias,
                    server_type.value,
                    command,
                    CommandStatus.PENDING.value,
                    iso(now),
                ),
            )
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
        )

    def get(self, command_id: CommandId) -> Command | None:
        """Fetch a command by ID, or None if not found."""
        with connect(self._db) as conn:
            row = conn.execute(
                """
                SELECT id, batch_id, position, server_alias, server_type,
                    command, status, result, approved_by, created_at, resolved_at
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
                    command, status, result, approved_by, created_at, resolved_at
                FROM commands WHERE batch_id = ?
                ORDER BY position ASC
                """,
                (batch_id,),
            ).fetchall()
        return [row_to_command(r) for r in rows]

    def update_status(
        self,
        command_id: CommandId,
        *,
        status: CommandStatus,
        approved_by: str | None = None,
        result: str | None = None,
    ) -> None:
        """Transition a command's status; stamp resolved_at iff status is terminal."""
        with connect(self._db) as conn:
            if status in _TERMINAL_STATUSES:
                _ = conn.execute(
                    """
                    UPDATE commands SET status = ?, approved_by = ?, result = ?, resolved_at = ?
                    WHERE id = ?
                    """,
                    (
                        status.value,
                        approved_by,
                        result,
                        iso(datetime.now(UTC)),
                        command_id,
                    ),
                )
            else:
                _ = conn.execute(
                    """
                    UPDATE commands SET status = ?, approved_by = ?, result = ?
                    WHERE id = ?
                    """,
                    (status.value, approved_by, result, command_id),
                )

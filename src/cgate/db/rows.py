"""SQLite row parsing: read raw rows and convert them into typed domain values."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cgate.db.types import (
    Batch,
    BatchId,
    Command,
    CommandId,
    CommandStatus,
    Connection,
    ServerType,
)

if TYPE_CHECKING:
    import sqlite3


def iso(dt: datetime) -> str:
    """Serialize a datetime to ISO-8601 UTC with a Z suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse(s: str) -> datetime:
    """Parse an ISO-8601 timestamp (Z suffix supported) into a tz-aware datetime."""
    return datetime.fromisoformat(s)


def col_str(row: sqlite3.Row, col: str) -> str:
    """Read a NOT NULL TEXT column from a row as str."""
    return str(row[col])


def col_opt_str(row: sqlite3.Row, col: str) -> str | None:
    """Read a nullable TEXT column from a row; None for SQL NULL."""
    value: object = row[col]
    if value is None:
        return None
    return str(value)


def col_int(row: sqlite3.Row, col: str) -> int:
    """Read an INTEGER column from a row as int."""
    return int(str(row[col]))


def col_opt_dt(row: sqlite3.Row, col: str) -> datetime | None:
    """Read a nullable TIMESTAMP column from a row as datetime."""
    raw: object = row[col]
    if raw is None:
        return None
    return parse(str(raw))


def row_to_batch(row: sqlite3.Row) -> Batch:
    """Convert a SQLite row into a typed Batch value object."""
    return Batch(
        id=BatchId(col_str(row, "id")),
        title=col_str(row, "title"),
        description=col_opt_str(row, "description"),
        requested_by_agent=col_opt_str(row, "requested_by_agent"),
        created_at=parse(col_str(row, "created_at")),
        resolved_at=col_opt_dt(row, "resolved_at"),
    )


def row_to_command(row: sqlite3.Row) -> Command:
    """Convert a SQLite row into a typed Command value object."""
    return Command(
        id=CommandId(col_str(row, "id")),
        batch_id=BatchId(col_str(row, "batch_id")),
        position=col_int(row, "position"),
        server_alias=col_str(row, "server_alias"),
        server_type=ServerType(col_str(row, "server_type")),
        command=col_str(row, "command"),
        status=CommandStatus(col_str(row, "status")),
        result=col_opt_str(row, "result"),
        approved_by=col_opt_str(row, "approved_by"),
        created_at=parse(col_str(row, "created_at")),
        resolved_at=col_opt_dt(row, "resolved_at"),
    )


def row_to_connection(row: sqlite3.Row) -> Connection:
    """Convert a SQLite row into a typed Connection value object."""
    return Connection(
        alias=col_str(row, "alias"),
        hostname=col_str(row, "hostname"),
        server_type=ServerType(col_str(row, "server_type")),
        detection_ssh=bool(col_int(row, "detection_ssh")),
        detection_winrm=bool(col_int(row, "detection_winrm")),
        created_at=parse(col_str(row, "created_at")),
    )

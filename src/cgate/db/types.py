"""Domain types for the database layer: enums, NewType IDs, and frozen dataclasses."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NewType

if TYPE_CHECKING:
    from datetime import datetime


class ServerType(StrEnum):
    """Type of a target server. Drives dialect (PowerShell vs Bash) and exec backend."""

    WINDOWS = "windows"
    LINUX = "linux"


class CommandStatus(StrEnum):
    """Lifecycle of a single command in a batch.

    Flow: PENDING -> APPROVED -> EXECUTED (or PENDING -> REJECTED / FAILED).
    APPROVED and PENDING are non-terminal (resolved_at IS NULL).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


BatchId = NewType("BatchId", str)
CommandId = NewType("CommandId", str)


@dataclass(frozen=True, slots=True)
class Batch:
    """A batch of related commands proposed together by one AI invocation."""

    id: BatchId
    title: str
    description: str | None
    requested_by_agent: str | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class Command:
    """A single command within a batch, addressed to one server."""

    id: CommandId
    batch_id: BatchId
    position: int
    server_alias: str
    server_type: ServerType
    command: str
    status: CommandStatus
    result: str | None
    approved_by: str | None
    created_at: datetime
    resolved_at: datetime | None

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

    Flow:
        PENDING -> APPROVED --[CAS claim]--> EXECUTING --[remote done]--> EXECUTED | FAILED
        PENDING -> REJECTED

    Terminal statuses (resolved_at is stamped, batch can resolve): EXECUTED,
    REJECTED, FAILED. Non-terminal: PENDING, EXECUTING. APPROVED sits between
    the human approval and the executor's CAS-claim; once the CAS succeeds,
    the row moves to EXECUTING and stays there for the duration of the
    remote call -- a separate process holding an EXECUTING row is the
    signal ``fail_orphaned_approvals`` uses to know the command is live
    (issues #36, #38).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
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
    """A single command within a batch, addressed to one server.

    ``claimed_at`` is set by the executor when it CAS-claims the row into
    EXECUTING (issues #36, #38). ``None`` for every other state. A live
    executor process keeps ``claimed_at`` recent (within its `timeout +
    grace`); a process that died mid-execution leaves it stale, which is
    how the heal detects orphaned EXECUTING rows.
    """

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
    claimed_at: datetime | None
    reason: str | None
    risk_label: str | None


@dataclass(frozen=True, slots=True)
class Connection:
    """A saved connection from this machine to a remote server, identified by alias."""

    alias: str
    hostname: str
    server_type: ServerType
    detection_ssh: bool
    detection_winrm: bool
    created_at: datetime

"""Domain types and exceptions for the executor layer."""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_TIMEOUT_SECONDS: Final = 30.0


class ErrorKind(StrEnum):
    """How a command execution failed (None on success)."""

    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    HOST_UNREACHABLE = "host_unreachable"
    PROTOCOL_ERROR = "protocol_error"


class ExecutorError(Exception):
    """Base for executor errors that should be surfaced as a structured result."""


class CommandTimeout(ExecutorError):  # noqa: N818 - public name required by executor API
    """Execution exceeded the timeout."""


class AuthenticationFailed(ExecutorError):  # noqa: N818 - public name required by executor API
    """Credentials were rejected by the target."""


class HostUnreachable(ExecutorError):  # noqa: N818 - public name required by executor API
    """The target host refused or failed the TCP connection."""


class ProtocolError(ExecutorError):
    """The remote server returned malformed output."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of running one command on one server.

    ``error_kind`` is None on any successful connection, regardless of exit code.
    Use ``ok`` to require both a zero exit code and no executor error.
    """

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    error_kind: ErrorKind | None

    @property
    def ok(self) -> bool:
        """Return true exactly when execution succeeded with a zero exit code."""
        return self.error_kind is None and self.exit_code == 0


def measure_duration_ms() -> tuple[int, Callable[[], int]]:
    """Return the monotonic start in milliseconds and an elapsed-time callable."""
    start = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - start) * 1000)

    return int(start * 1000), elapsed

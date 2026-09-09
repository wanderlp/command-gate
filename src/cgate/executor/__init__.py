"""Remote command execution: WinRM for Windows, SSH for Linux."""
from __future__ import annotations

from cgate.executor.base import DEFAULT_TIMEOUT_SECONDS, ErrorKind, ExecutionResult
from cgate.executor.selector import execute_command

__all__ = ["DEFAULT_TIMEOUT_SECONDS", "ErrorKind", "ExecutionResult", "execute_command"]

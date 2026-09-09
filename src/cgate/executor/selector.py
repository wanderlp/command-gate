"""Top-level dispatch for executing a command on a saved connection."""
from __future__ import annotations

from cgate.connections.auth import StoredCredential, get_credential, is_kerberos_available
from cgate.db.types import Connection, ServerType
from cgate.executor.base import DEFAULT_TIMEOUT_SECONDS, ErrorKind, ExecutionResult
from cgate.executor.ssh import execute_linux
from cgate.executor.winrm import execute_windows


def execute_command(
    connection: Connection,
    command: str,
    *,
    credential: StoredCredential | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ExecutionResult:
    """Execute a command on a saved connection using its selected backend."""
    resolved_credential = credential
    if resolved_credential is None:
        resolved_credential = get_credential(connection.alias)
    if resolved_credential is None:
        return _missing_credential_result(connection)

    match connection.server_type:
        case ServerType.WINDOWS:
            return execute_windows(
                connection.hostname,
                command,
                resolved_credential,
                timeout=timeout,
            )
        case ServerType.LINUX:
            return execute_linux(
                connection.hostname,
                command,
                resolved_credential,
                timeout=timeout,
            )


def _missing_credential_result(connection: Connection) -> ExecutionResult:
    match connection.server_type:
        case ServerType.WINDOWS:
            kerberos_available = is_kerberos_available()
        case ServerType.LINUX:
            kerberos_available = False
    if kerberos_available:
        message = (
            "kerberos passthrough available, but Phase 1 doesn't ship kerberos transport; "
            "store an NTLM credential with `cgate connections add` first."
        )
    else:
        message = f"no credential for alias '{connection.alias}'"
    return ExecutionResult(
        stdout="",
        stderr=message,
        exit_code=-1,
        duration_ms=0,
        error_kind=ErrorKind.AUTH_FAILED,
    )

"""Linux command execution over SSH (paramiko)."""
from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Final

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError

from cgate.executor.base import DEFAULT_TIMEOUT_SECONDS, ErrorKind, ExecutionResult

if TYPE_CHECKING:
    from cgate.connections.auth import StoredCredential

SSH_PORT: Final = 22


def execute_linux(
    hostname: str,
    command: str,
    credential: StoredCredential,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ExecutionResult:
    """Run a command on a host via SSH using a key before a password."""
    started = time.monotonic()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(
        paramiko.AutoAddPolicy()  # noqa: S507 - Phase 1 explicitly trusts unknown host keys
    )

    try:
        if credential.ssh_key:
            client.connect(
                hostname,
                port=SSH_PORT,
                username=credential.username,
                key_filename=credential.ssh_key,
                timeout=timeout,
                auth_timeout=timeout,
                banner_timeout=timeout,
            )
        elif credential.password:
            client.connect(
                hostname,
                port=SSH_PORT,
                username=credential.username,
                password=credential.password,
                timeout=timeout,
                auth_timeout=timeout,
                banner_timeout=timeout,
            )
        else:
            return _failure(
                "credential has neither ssh_key nor password",
                ErrorKind.AUTH_FAILED,
                started,
            )

        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdout.channel.settimeout(timeout)
        try:
            out_bytes = stdout.read()
            err_bytes = stderr.read()
            exit_code = stdout.channel.recv_exit_status()
        except Exception as exc:  # noqa: BLE001 - Paramiko streams expose generic read errors
            return _failure(str(exc), ErrorKind.TIMEOUT, started)

        return ExecutionResult(
            stdout=_decode_output(out_bytes),
            stderr=_decode_output(err_bytes),
            exit_code=int(exit_code),
            duration_ms=int((time.monotonic() - started) * 1000),
            error_kind=None,
        )
    except Exception as exc:  # noqa: BLE001 - Paramiko connect can surface socket subclasses
        return _failure_from_paramiko_exception(exc, started)
    finally:
        with contextlib.suppress(Exception):
            client.close()


def _decode_output(output: str | bytes | bytearray | None) -> str:
    if isinstance(output, (bytes, bytearray)):
        return output.decode("utf-8", errors="replace")
    return output or ""


def _failure(message: str, error_kind: ErrorKind, started: float) -> ExecutionResult:
    return ExecutionResult(
        stdout="",
        stderr=message,
        exit_code=-1,
        duration_ms=int((time.monotonic() - started) * 1000),
        error_kind=error_kind,
    )


def _failure_from_paramiko_exception(exc: Exception, started: float) -> ExecutionResult:
    return _failure(str(exc), _classify_paramiko_exception(exc), started)


def _classify_paramiko_exception(exc: Exception) -> ErrorKind:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if isinstance(exc, paramiko.AuthenticationException) or "auth" in name:
        return ErrorKind.AUTH_FAILED
    if isinstance(exc, NoValidConnectionsError):
        return ErrorKind.HOST_UNREACHABLE
    if any(
        signal in message
        for signal in ("no route to host", "connection refused", "getaddrinfo failed")
    ):
        return ErrorKind.HOST_UNREACHABLE
    if "timed out" in message or "timeout" in name:
        return ErrorKind.TIMEOUT
    return ErrorKind.PROTOCOL_ERROR

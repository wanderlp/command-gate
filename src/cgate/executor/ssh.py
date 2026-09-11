"""Linux command execution over SSH (paramiko)."""
from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Final

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError
from typing_extensions import override

from cgate.core.paths import data_dir
from cgate.executor.base import DEFAULT_TIMEOUT_SECONDS, ErrorKind, ExecutionResult

if TYPE_CHECKING:
    from pathlib import Path

    from cgate.connections.auth import StoredCredential

SSH_PORT: Final = 22


class _TrustOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    """Accept an unknown host key once, then pin it to ``known_hosts_path``.

    Unlike ``AutoAddPolicy`` (issue #6), which trusts every connection with
    no memory at all, paramiko only calls ``missing_host_key`` when the
    hostname isn't already present in the client's loaded host keys --
    if it IS present but the presented key doesn't match, paramiko raises
    ``BadHostKeyException`` on its own before this policy is ever
    consulted. So preloading a persistent file and only auto-trusting
    truly new hosts here gives real trust-on-first-use: a key that
    changes after that first connection (MITM, or the box got rebuilt)
    is rejected instead of silently accepted again.
    """

    _known_hosts_path: Path  # class-level annotation required by strict mode

    def __init__(self, known_hosts_path: Path) -> None:
        """Remember where to persist newly-trusted host keys."""
        self._known_hosts_path = known_hosts_path

    @override
    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        """Trust and persist a host key seen for the first time."""
        client.get_host_keys().add(hostname, key.get_name(), key)
        self._known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(self._known_hosts_path))


def _known_hosts_path() -> Path:
    return data_dir() / "known_hosts"


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
    known_hosts_path = _known_hosts_path()
    if known_hosts_path.exists():
        client.load_host_keys(str(known_hosts_path))
    client.set_missing_host_key_policy(_TrustOnFirstUsePolicy(known_hosts_path))

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

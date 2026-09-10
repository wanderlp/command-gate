"""Windows command execution via WinRM (pywinrm)."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

import winrm

from cgate.executor.base import DEFAULT_TIMEOUT_SECONDS, ErrorKind, ExecutionResult

if TYPE_CHECKING:
    from cgate.connections.auth import StoredCredential

WINRM_HTTP_PORT: Final = 5985
WINRM_HTTPS_PORT: Final = 5986


def _endpoint(hostname: str, *, https: bool) -> str:
    port = WINRM_HTTPS_PORT if https else WINRM_HTTP_PORT
    scheme = "https" if https else "http"
    return f"{scheme}://{hostname}:{port}/wsman"


def execute_windows(
    hostname: str,
    command: str,
    credential: StoredCredential,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    https: bool = True,
) -> ExecutionResult:
    """Run a command on a host over WinRM with NTLM authentication.

    Prefers the protocol requested by the caller (defaults to HTTPS for
    the secure WinRM config) but falls back to the other protocol when
    the primary listener is unreachable. WinRM operators commonly run
    HTTP on port 5985 for internal/lab hosts where TLS is not wired
    up, so a connection refused on 5986 should not be fatal when 5985
    is available.
    """
    started = time.monotonic()
    last_exc: Exception | None = None
    for attempt_https in (https, not https):
        try:
            session = winrm.Session(
                _endpoint(hostname, https=attempt_https),
                auth=(credential.username, credential.password or ""),
                transport="ntlm",
                # pywinrm requires ``read_timeout_sec > operation_timeout_sec``;
                # otherwise it raises ``read_timeout_sec must exceed
                # operation_timeout_sec``. The operation budget covers the
                # round-trip + remote execution; the read budget also has
                # to cover pulling the response back, so we add a fixed
                # margin.
                operation_timeout_sec=timeout,
                read_timeout_sec=timeout + 30,
            )
            response = session.run_cmd(command)
            return ExecutionResult(
                stdout=_decode_output(response.std_out),
                stderr=_decode_output(response.std_err),
                exit_code=int(response.status_code),
                duration_ms=int((time.monotonic() - started) * 1000),
                error_kind=None,
            )
        except Exception as exc:  # noqa: BLE001 - SDK errors lack a closed common hierarchy
            last_exc = exc
            continue

    assert last_exc is not None  # loop ran at least once
    return _failure_from_winrm_exception(last_exc, started)


def _decode_output(output: str | bytes | bytearray | None) -> str:
    if isinstance(output, (bytes, bytearray)):
        return output.decode("utf-8", errors="replace")
    return output or ""


def _failure_from_winrm_exception(exc: Exception, started: float) -> ExecutionResult:
    return ExecutionResult(
        stdout="",
        stderr=str(exc),
        exit_code=-1,
        duration_ms=int((time.monotonic() - started) * 1000),
        error_kind=_classify_winrm_exception(exc),
    )


def _classify_winrm_exception(exc: Exception) -> ErrorKind:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if any(signal in message for signal in ("401", "unauthorized", "auth", "credential")):
        return ErrorKind.AUTH_FAILED
    if "timed out" in message or "timeout" in name:
        return ErrorKind.TIMEOUT
    if any(
        signal in message
        for signal in (
            "connection refused",
            "no route to host",
            "getaddrinfo failed",
            "name or service not known",
            "network is unreachable",
        )
    ):
        return ErrorKind.HOST_UNREACHABLE
    return ErrorKind.PROTOCOL_ERROR

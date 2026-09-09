"""TCP probe to detect whether a remote host is Windows (WinRM) or Linux (SSH).

Pure logic, no DB access, no keyring. Tests can mock socket.create_connection.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass

from cgate.db.types import ServerType

SSH_PORT = 22
WINRM_HTTP_PORT = 5985
WINRM_HTTPS_PORT = 5986
DEFAULT_PROBE_TIMEOUT = 2.0


class AmbiguousHostError(Exception):
    """Host responded to BOTH SSH and WinRM; user must choose."""


class UnknownHostError(Exception):
    """Host did not respond to either SSH or WinRM."""


def _probe(hostname: str, port: int, timeout: float) -> bool:
    """Try TCP connect and report whether the port is open within the timeout."""
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class DetectionProbe:
    """Result of probing a host for SSH and WinRM."""

    hostname: str
    ssh: bool
    winrm: bool

    @property
    def server_type(self) -> ServerType:
        """Resolve the result or raise when the host is ambiguous or unknown."""
        if self.ssh and self.winrm:
            message = " ".join(
                (
                    f"{self.hostname} responded to both SSH (22) and WinRM",
                    "(5985/5986). Specify which to use.",
                )
            )
            raise AmbiguousHostError(message)
        if self.ssh:
            return ServerType.LINUX
        if self.winrm:
            return ServerType.WINDOWS
        message = " ".join(
            (
                f"{self.hostname} did not respond to SSH (22) or WinRM",
                "(5985/5986).",
            )
        )
        raise UnknownHostError(message)


def probe_host(
    hostname: str, *, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> DetectionProbe:
    """Probe the host for SSH and WinRM and return both binary signals."""
    ssh = _probe(hostname, SSH_PORT, timeout)
    winrm_https = _probe(hostname, WINRM_HTTPS_PORT, timeout)
    winrm_http = _probe(hostname, WINRM_HTTP_PORT, timeout)
    return DetectionProbe(
        hostname=hostname,
        ssh=ssh,
        winrm=winrm_https or winrm_http,
    )

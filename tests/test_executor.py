from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import paramiko

from cgate.connections.auth import StoredCredential
from cgate.db.types import Connection, ServerType
from cgate.executor.base import ErrorKind, ExecutionResult
from cgate.executor.selector import execute_command
from cgate.executor.ssh import execute_linux
from cgate.executor.winrm import execute_windows


def _credential(*, has_password: bool = True, ssh_key: str | None = None) -> StoredCredential:
    username = "operator"
    password = str(len(username)) if has_password else None
    return StoredCredential(username=username, password=password, ssh_key=ssh_key)


def _connection(server_type: ServerType) -> Connection:
    return Connection(
        alias="server",
        hostname="server.example",
        server_type=server_type,
        detection_ssh=server_type is ServerType.LINUX,
        detection_winrm=server_type is ServerType.WINDOWS,
        created_at=datetime.now(UTC),
    )


def _successful_result() -> ExecutionResult:
    return ExecutionResult("output", "", 0, 1, None)


def test_result_ok_true_when_no_error_and_zero_exit() -> None:
    result = ExecutionResult("", "", 0, 1, None)
    assert result.ok is True


def test_result_ok_false_when_nonzero_exit() -> None:
    result = ExecutionResult("", "failed", 1, 1, None)
    assert result.ok is False


def test_result_ok_false_when_error_kind_set() -> None:
    result = ExecutionResult("", "timed out", -1, 1, ErrorKind.TIMEOUT)
    assert result.ok is False


def test_execute_windows_happy_path_returns_zero_exit_code() -> None:
    session_factory = MagicMock()
    response = MagicMock(std_out=b"hello\r\n", std_err=b"", status_code=0)
    session_factory.return_value.run_cmd.return_value = response

    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows("10.0.0.1", "Get-Service", _credential())

    assert result.ok is True
    assert result.exit_code == 0
    assert "hello" in result.stdout
    session_factory.assert_called_once_with(
        "https://10.0.0.1:5986/wsman",
        auth=("operator", "8"),
        transport="ntlm",
        read_timeout_sec=30.0,
        operation_timeout_sec=30.0,
    )


def test_execute_windows_auth_failure_sets_auth_failed() -> None:
    session_factory = MagicMock(side_effect=Exception("401 Unauthorized credentials"))
    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows("windows.example", "hostname", _credential())
    assert result.error_kind is ErrorKind.AUTH_FAILED


def test_execute_windows_unreachable_host_sets_host_unreachable() -> None:
    session_factory = MagicMock(side_effect=OSError("connection refused"))
    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows("offline.example", "hostname", _credential())
    assert result.error_kind is ErrorKind.HOST_UNREACHABLE


def test_execute_windows_timeout_sets_timeout() -> None:
    session_factory = MagicMock(side_effect=TimeoutError("operation timed out"))
    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows("slow.example", "hostname", _credential())
    assert result.error_kind is ErrorKind.TIMEOUT


def test_execute_linux_happy_path_returns_zero_exit_code() -> None:
    client_factory = MagicMock()
    stdin, stdout, stderr = MagicMock(), MagicMock(), MagicMock()
    stdout.read.return_value = b"hello\n"
    stderr.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    client_factory.return_value.exec_command.return_value = (stdin, stdout, stderr)

    with patch("cgate.executor.ssh.paramiko.SSHClient", client_factory):
        result = execute_linux("10.0.0.2", "uptime", _credential())

    assert result.ok is True
    assert result.stdout == "hello\n"
    client_factory.return_value.close.assert_called_once_with()


def test_execute_linux_uses_ssh_key_when_provided() -> None:
    client_factory = MagicMock()
    stdout, stderr = MagicMock(), MagicMock()
    stdout.read.return_value = b""
    stderr.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    client_factory.return_value.exec_command.return_value = (MagicMock(), stdout, stderr)

    with patch("cgate.executor.ssh.paramiko.SSHClient", client_factory):
        _ = execute_linux("linux.example", "true", _credential(ssh_key="id_ed25519"))

    client_factory.return_value.connect.assert_called_once_with(
        "linux.example",
        port=22,
        username="operator",
        key_filename="id_ed25519",
        timeout=30.0,
        auth_timeout=30.0,
        banner_timeout=30.0,
    )


def test_execute_linux_auth_failure_sets_auth_failed() -> None:
    client_factory = MagicMock()
    client_factory.return_value.connect.side_effect = paramiko.AuthenticationException("denied")
    with patch("cgate.executor.ssh.paramiko.SSHClient", client_factory):
        result = execute_linux("linux.example", "id", _credential())
    assert result.error_kind is ErrorKind.AUTH_FAILED


def test_execute_linux_unreachable_host_sets_host_unreachable() -> None:
    client_factory = MagicMock()
    client_factory.return_value.connect.side_effect = OSError("No route to host")
    with patch("cgate.executor.ssh.paramiko.SSHClient", client_factory):
        result = execute_linux("offline.example", "id", _credential())
    assert result.error_kind is ErrorKind.HOST_UNREACHABLE


def test_execute_linux_missing_credential_both_password_and_key_returns_auth_failed() -> None:
    client_factory = MagicMock()
    with patch("cgate.executor.ssh.paramiko.SSHClient", client_factory):
        result = execute_linux("linux.example", "id", _credential(has_password=False))
    assert result.error_kind is ErrorKind.AUTH_FAILED
    client_factory.return_value.connect.assert_not_called()
    client_factory.return_value.close.assert_called_once_with()


def test_execute_command_picks_windows_for_windows_type() -> None:
    expected = _successful_result()
    with (
        patch("cgate.executor.selector.execute_windows", return_value=expected) as windows,
        patch("cgate.executor.selector.execute_linux") as linux,
    ):
        result = execute_command(
            _connection(ServerType.WINDOWS), "hostname", credential=_credential()
        )
    assert result is expected
    windows.assert_called_once_with("server.example", "hostname", _credential(), timeout=30.0)
    linux.assert_not_called()


def test_execute_command_picks_linux_for_linux_type() -> None:
    expected = _successful_result()
    with (
        patch("cgate.executor.selector.execute_windows") as windows,
        patch("cgate.executor.selector.execute_linux", return_value=expected) as linux,
    ):
        result = execute_command(_connection(ServerType.LINUX), "uptime", credential=_credential())
    assert result is expected
    linux.assert_called_once_with("server.example", "uptime", _credential(), timeout=30.0)
    windows.assert_not_called()


def test_execute_command_auto_loads_credential_from_keyring_when_not_provided() -> None:
    credential = _credential()
    expected = _successful_result()
    with (
        patch("cgate.executor.selector.get_credential", return_value=credential) as load,
        patch("cgate.executor.selector.execute_windows", return_value=expected) as windows,
    ):
        result = execute_command(_connection(ServerType.WINDOWS), "hostname")
    assert result is expected
    load.assert_called_once_with("server")
    windows.assert_called_once_with("server.example", "hostname", credential, timeout=30.0)

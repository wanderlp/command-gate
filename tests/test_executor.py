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
    session_factory.return_value.run_ps.return_value = response

    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows("10.0.0.1", "Get-Service", _credential())

    assert result.ok is True
    assert result.exit_code == 0
    assert "hello" in result.stdout
    session_factory.assert_called_once_with(
        "https://10.0.0.1:5986/wsman",
        auth=("operator", "8"),
        transport="ntlm",
        # pywinrm requires read_timeout_sec > operation_timeout_sec;
        # cgate passes timeout + 30. See test below for the regression.
        operation_timeout_sec=30.0,
        read_timeout_sec=60.0,
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


def test_execute_windows_passes_read_timeout_strictly_greater_than_operation() -> None:
    """pywinrm rejects ``read_timeout_sec <= operation_timeout_sec`` at
    Session construction time with ``read_timeout_sec must exceed
    operation_timeout_sec``. Regression: prior code passed the same
    value for both, which made every WinRM execute crash before any
    network round-trip. The two timeouts must always satisfy
    ``read > operation`` regardless of the user-supplied timeout.
    """
    session_factory = MagicMock()
    response = MagicMock()
    response.std_out = ""
    response.std_err = ""
    response.status_code = 0
    session_factory.return_value.run_ps.return_value = response

    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        for timeout in (5.0, 30.0, 120.0):
            execute_windows(
                "win.example", "hostname", _credential(), timeout=timeout
            )

    assert session_factory.call_count == 3
    for call in session_factory.call_args_list:
        kwargs = call.kwargs
        assert kwargs["read_timeout_sec"] > kwargs["operation_timeout_sec"], (
            f"read_timeout_sec={kwargs['read_timeout_sec']} must exceed "
            f"operation_timeout_sec={kwargs['operation_timeout_sec']}"
        )
        # The margin must be non-zero (no equality regression).
        assert kwargs["read_timeout_sec"] != kwargs["operation_timeout_sec"]


def test_execute_windows_falls_back_to_http_when_https_unreachable() -> None:
    """WinRM operators commonly run HTTP-only listeners (port 5985) on
    internal hosts. cgate should try the caller's preferred protocol
    first, then fall back to the other one so a refused HTTPS
    connection does not fail the whole execute when HTTP would have
    worked.
    """
    session_factory = MagicMock()

    def make_session(endpoint, **kwargs):
        if endpoint.startswith("https://"):
            raise OSError("HTTPSConnection: connection refused")
        s = MagicMock()
        s.run_ps.return_value = MagicMock(
            std_out="hello\n", std_err="", status_code=0
        )
        return s

    session_factory.side_effect = make_session

    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows("http-host.example", "Get-Service", _credential())

    assert result.ok is True
    assert result.stdout == "hello\n"
    # Two attempts: first https (refused), then http (succeeded).
    assert session_factory.call_count == 2
    endpoints = [call.args[0] for call in session_factory.call_args_list]
    assert endpoints == [
        "https://http-host.example:5986/wsman",
        "http://http-host.example:5985/wsman",
    ]


def test_execute_windows_falls_back_to_https_when_http_unreachable() -> None:
    """Mirror of the above: caller requests HTTP, but the server only
    exposes HTTPS. The fallback must work in either direction.
    """
    session_factory = MagicMock()

    def make_session(endpoint, **kwargs):
        if endpoint.startswith("http://"):
            raise OSError("HTTPConnection: connection refused")
        s = MagicMock()
        s.run_ps.return_value = MagicMock(
            std_out="hello\n", std_err="", status_code=0
        )
        return s

    session_factory.side_effect = make_session

    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows(
            "https-host.example", "Get-Service", _credential(), https=False
        )

    assert result.ok is True
    endpoints = [call.args[0] for call in session_factory.call_args_list]
    # Caller asked for HTTP first; HTTPS is the fallback.
    assert endpoints == [
        "http://https-host.example:5985/wsman",
        "https://https-host.example:5986/wsman",
    ]


def test_execute_windows_uses_powershell_not_cmd() -> None:
    """Regression: cgate executes Windows commands via ``run_ps`` (PowerShell),
    not ``run_cmd`` (legacy cmd.exe). PowerShell supports both PowerShell
    cmdlets and classic cmd-line utilities, so using it as the default
    avoids the ``'Get-ChildItem' is not recognized as an internal or
    external command`` failure users saw when their commands were
    PowerShell-only.
    """
    session_factory = MagicMock()
    response = MagicMock(std_out="ok", std_err="", status_code=0)
    session_factory.return_value.run_ps.return_value = response

    with patch("cgate.executor.winrm.winrm.Session", session_factory):
        result = execute_windows("host.example", "Get-ChildItem C:\\", _credential())

    session_factory.return_value.run_ps.assert_called_once_with(
        "Get-ChildItem C:\\"
    )
    session_factory.return_value.run_cmd.assert_not_called()
    assert result.ok is True

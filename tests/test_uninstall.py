"""Tests for the `cgate uninstall` top-level command."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cgate.cli.main import app
from cgate.db.types import Connection, ServerType
from cgate.mcp_installer import ClientInstall


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_client() -> ClientInstall:
    return ClientInstall(
        name="opencode",
        label="opencode",
        config_path=Path("/tmp/opencode.jsonc"),
    )


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Wire the uninstall command to operate on tmp_path instead of the
    real machine: a fake binary, a fake data dir, and an empty CGATE_DB_PATH.
    """
    binary = tmp_path / "cgate.exe"
    binary.write_bytes(b"fake")
    data = tmp_path / "data"
    data.mkdir()
    db = data / "cgate.db"

    monkeypatch.setenv("CGATE_DB_PATH", str(db))
    monkeypatch.setattr("cgate.cli.uninstall.current_binary_path", lambda: binary)
    monkeypatch.setattr("cgate.cli.uninstall.data_dir", lambda: data)
    monkeypatch.setattr("cgate.cli.uninstall.detect_clients", lambda: [])
    return {"binary": binary, "data": data, "db": db}


def test_uninstall_help_lists_every_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["uninstall", "--help"])
    assert result.exit_code == 0
    for flag in ("--binary", "--data", "--mcp", "--yes"):
        assert flag in result.stdout


def test_uninstall_default_scope_removes_everything(
    runner: CliRunner, isolated_env: dict
) -> None:
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "Will remove:" in result.stdout
    assert not isolated_env["binary"].exists()
    assert not isolated_env["data"].exists()


def test_uninstall_binary_flag_only_deletes_binary(
    runner: CliRunner, isolated_env: dict
) -> None:
    result = runner.invoke(app, ["uninstall", "--binary", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert not isolated_env["binary"].exists()
    assert isolated_env["data"].exists()


def test_uninstall_data_flag_only_deletes_data(
    runner: CliRunner, isolated_env: dict
) -> None:
    result = runner.invoke(app, ["uninstall", "--data", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert isolated_env["binary"].exists()
    assert not isolated_env["data"].exists()


def test_uninstall_mcp_flag_only_unregisters_mcp(
    runner: CliRunner, isolated_env: dict, fake_client: ClientInstall
) -> None:
    detect_called: list[bool] = []

    def fake_detect() -> list[ClientInstall]:
        detect_called.append(True)
        return [fake_client]

    with (
        patch("cgate.cli.uninstall.detect_clients", fake_detect),
        patch("cgate.cli.uninstall.is_registered", lambda _client: True),
        patch("cgate.cli.uninstall.unregister") as unregister_mock,
    ):
        result = runner.invoke(app, ["uninstall", "--mcp", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert detect_called, "detect_clients should be consulted"
    unregister_mock.assert_called_once_with(fake_client)
    assert isolated_env["binary"].exists()
    assert isolated_env["data"].exists()


def test_uninstall_no_targets_prints_nothing_to_do(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No frozen binary, no data dir, no MCP clients -- nothing actionable.
    monkeypatch.setattr("cgate.cli.uninstall.current_binary_path", lambda: None)
    monkeypatch.setattr("cgate.cli.uninstall.data_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr("cgate.cli.uninstall.detect_clients", lambda: [])
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Nothing to do" in result.stdout


def test_uninstall_binary_handles_permission_error(
    runner: CliRunner, isolated_env: dict
) -> None:
    binary = isolated_env["binary"]

    real_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == binary:
            raise PermissionError(13, "locked", str(self))
        real_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", fake_unlink):
        result = runner.invoke(app, ["uninstall", "--binary", "--yes"])

    assert result.exit_code == 0
    assert "Could not delete binary" in result.stdout
    assert "Close any running cgate" in result.stdout
    assert binary.exists()


def test_uninstall_binary_skips_when_not_frozen(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cgate.cli.uninstall.current_binary_path", lambda: None)
    monkeypatch.setattr("cgate.cli.uninstall.data_dir", lambda: Path("/nonexistent"))
    monkeypatch.setattr("cgate.cli.uninstall.detect_clients", lambda: [])

    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "Not running from a PyInstaller binary" in result.stdout


def test_uninstall_data_handles_missing_dir(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "missing"
    monkeypatch.setattr("cgate.cli.uninstall.current_binary_path", lambda: None)
    monkeypatch.setattr("cgate.cli.uninstall.data_dir", lambda: data)
    monkeypatch.setattr("cgate.cli.uninstall.detect_clients", lambda: [])

    result = runner.invoke(app, ["uninstall", "--data", "--yes"])
    assert result.exit_code == 0
    assert "already gone" in result.stdout


def test_uninstall_yes_flag_skips_prompts(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single --yes covers every step; typer.confirm would otherwise block."""
    monkeypatch.setattr("cgate.cli.uninstall.current_binary_path",
                        lambda: isolated_env["binary"])
    result = runner.invoke(app, ["uninstall", "--binary", "--data", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "Skipped" not in result.stdout


def test_uninstall_without_yes_prompts_and_aborts_on_no(
    runner: CliRunner, isolated_env: dict
) -> None:
    # Empty stdin -> typer.confirm falls back to its default (False for
    # destructive prompts). The binary must remain on disk.
    result = runner.invoke(app, ["uninstall", "--binary"], input="")
    assert result.exit_code != 0
    assert isolated_env["binary"].exists()


def test_uninstall_without_yes_accepts_y_confirmation(
    runner: CliRunner, isolated_env: dict
) -> None:
    result = runner.invoke(app, ["uninstall", "--binary"], input="y\n")
    assert result.exit_code == 0, result.stdout
    assert not isolated_env["binary"].exists()


def test_uninstall_mcp_skips_when_user_declines(
    runner: CliRunner, isolated_env: dict, fake_client: ClientInstall
) -> None:
    with (
        patch("cgate.cli.uninstall.detect_clients", lambda: [fake_client]),
        patch("cgate.cli.uninstall.is_registered", lambda _c: True),
        patch("cgate.cli.uninstall.unregister") as unregister_mock,
    ):
        result = runner.invoke(app, ["uninstall", "--mcp"], input="n\n")

    assert result.exit_code == 0
    unregister_mock.assert_not_called()
    assert "Skipped opencode" in result.stdout


def test_uninstall_mcp_reports_failure_when_unregister_raises(
    runner: CliRunner, isolated_env: dict, fake_client: ClientInstall
) -> None:
    def fake_unregister(_client: ClientInstall) -> bool:
        raise OSError(13, "permission denied")

    with (
        patch("cgate.cli.uninstall.detect_clients", lambda: [fake_client]),
        patch("cgate.cli.uninstall.is_registered", lambda _c: True),
        patch("cgate.cli.uninstall.unregister", fake_unregister),
    ):
        result = runner.invoke(app, ["uninstall", "--mcp", "--yes"])

    assert result.exit_code == 0
    assert "Failed to unregister" in result.stdout


def test_uninstall_data_removes_keyring_entries(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a connection is in the DB, its keyring entry must be removed too."""
    removed: list[str] = []

    def fake_remove_credential(alias: str) -> None:
        removed.append(alias)

    monkeypatch.setattr("cgate.cli.uninstall.remove_credential", fake_remove_credential)

    fake_conn = Connection(
        alias="srv-test",
        hostname="example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
        created_at=datetime.now(UTC),
    )

    from cgate.cli import uninstall as uninstall_module

    monkeypatch.setattr(
        uninstall_module.ConnectionsRepo, "__init__", lambda self, _db: None
    )
    monkeypatch.setattr(
        uninstall_module.ConnectionsRepo, "list_all", lambda self: [fake_conn]
    )

    result = runner.invoke(app, ["uninstall", "--data", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert removed == ["srv-test"]
    assert not isolated_env["data"].exists()

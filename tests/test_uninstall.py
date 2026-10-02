"""Tests for the `cgate uninstall` top-level command."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cgate.cli import uninstall as uninstall_module
from cgate.cli.main import app
from cgate.cli.uninstall import _spawn_delayed_delete
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


def test_uninstall_binary_handles_permission_error_non_windows(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Windows there is no self-lock concept, so a PermissionError is a
    genuine failure and must be reported plainly."""
    binary = isolated_env["binary"]
    monkeypatch.setattr(sys, "platform", "linux")

    real_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == binary:
            raise PermissionError(13, "locked", str(self))
        real_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", fake_unlink):
        result = runner.invoke(app, ["uninstall", "--binary", "--yes"])

    assert result.exit_code == 0
    assert "Could not delete binary" in result.stdout
    assert binary.exists()


def test_uninstall_binary_self_lock_defers_delete_on_windows(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, a locked binary with no other blocking cgate process is
    assumed to be locked by this process itself (it always is, since the
    running exe holds its own image open). Uninstall should hand off to a
    background delete helper instead of reporting a hard failure -- and the
    final summary must say so, not print a bare "Uninstall complete."."""
    binary = isolated_env["binary"]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "cgate.cli.uninstall.find_blocking_processes", lambda *a, **k: []
    )
    # No compiled helper available locally or fetchable -- force the
    # shell-chain fallback deterministically instead of letting
    # ensure_helper_binary make a real network call in a test.
    monkeypatch.setattr("cgate.cli.uninstall.ensure_helper_binary", lambda *a, **k: None)
    spawned: list[Path] = []
    monkeypatch.setattr(
        "cgate.cli.uninstall._spawn_delayed_delete",
        lambda target: spawned.append(target) or True,
    )

    real_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == binary:
            raise PermissionError(13, "locked", str(self))
        real_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", fake_unlink):
        result = runner.invoke(app, ["uninstall", "--binary", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert spawned == [binary]
    flat_stdout = " ".join(result.stdout.split())
    assert "will be deleted automatically" in flat_stdout
    assert "Uninstall complete" in flat_stdout
    assert binary.exists()  # deletion is done by the (mocked) background helper


def test_uninstall_binary_prefers_compiled_helper_when_available(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a compiled helper is available, it must be used instead of the
    cmd.exe shell-chain fallback."""
    binary = isolated_env["binary"]
    helper = binary.with_name("cgate-helper.exe")
    monkeypatch.setattr(sys, "platform", "win32")
    # append_log resolves data_dir() at call time -- without this, a
    # successful (mocked) spawn writes a real line to the machine's
    # actual update.log instead of staying inside isolated_env's tmp_path.
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: isolated_env["data"])
    monkeypatch.setattr("cgate.cli.uninstall.find_blocking_processes", lambda *a, **k: [])
    monkeypatch.setattr("cgate.cli.uninstall.ensure_helper_binary", lambda *a, **k: helper)
    spawn_calls: list[tuple] = []
    monkeypatch.setattr(
        "cgate.cli.uninstall.spawn_helper",
        lambda _helper, *args: spawn_calls.append(args) or True,
    )

    real_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == binary:
            raise PermissionError(13, "locked", str(self))
        real_unlink(self, *args, **kwargs)

    with (
        patch.object(Path, "unlink", fake_unlink),
        patch("cgate.cli.uninstall._spawn_delayed_delete") as fallback,
    ):
        result = runner.invoke(app, ["uninstall", "--binary", "--yes"])

    assert result.exit_code == 0, result.stdout
    fallback.assert_not_called()
    assert spawn_calls == [("delete", "--target", str(binary), "--wait-pid", str(os.getpid()))]


def test_uninstall_binary_kills_other_blocker_and_retries(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different cgate process (not us) holding the lock should be offered
    for a kill-and-retry, same as `update apply` does, rather than assumed
    to be our own self-lock."""
    binary = isolated_env["binary"]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "cgate.cli.uninstall.find_blocking_processes", lambda *a, **k: [4321]
    )
    monkeypatch.setattr(
        "cgate.cli.uninstall.find_mcp_serving_pids", lambda pids: []
    )
    killed: list[int] = []
    monkeypatch.setattr(
        "cgate.cli.uninstall.kill_process",
        lambda pid: killed.append(pid) or True,
    )
    monkeypatch.setattr("cgate.cli.uninstall.time.sleep", lambda _s: None)

    real_unlink = Path.unlink
    attempts = {"count": 0}

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == binary:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise PermissionError(13, "locked", str(self))
            return
        real_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", fake_unlink):
        result = runner.invoke(app, ["uninstall", "--binary", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert killed == [4321]
    assert attempts["count"] == 2


def test_uninstall_binary_removes_sibling_helper_once_unlocked(
    runner: CliRunner, isolated_env: dict
) -> None:
    """Once cgate.exe is actually gone (no self-lock), the orphaned
    cgate-helper.exe next to it should be cleaned up too -- it's an
    implementation detail the user never installed by hand."""
    binary = isolated_env["binary"]
    helper = binary.with_name("cgate-helper.exe")
    helper.write_bytes(b"helper binary")

    result = runner.invoke(app, ["uninstall", "--binary", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert not binary.exists()
    assert not helper.exists()


def test_uninstall_binary_defers_helper_cleanup_when_helper_still_locked(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the helper file can't be unlinked directly (most likely
    because it's the very process performing a deferred delete of
    cgate.exe right now), fall back to the cmd.exe shell-chain instead of
    failing or leaving it there silently."""
    binary = isolated_env["binary"]
    helper = binary.with_name("cgate-helper.exe")
    helper.write_bytes(b"helper binary")

    monkeypatch.setattr(sys, "platform", "win32")
    # append_log resolves data_dir() at call time -- without this, the
    # (mocked) successful helper dispatch for the main binary writes a
    # real line to the machine's actual update.log.
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: isolated_env["data"])
    monkeypatch.setattr("cgate.cli.uninstall.find_blocking_processes", lambda *a, **k: [])
    monkeypatch.setattr("cgate.cli.uninstall.ensure_helper_binary", lambda *a, **k: helper)
    monkeypatch.setattr("cgate.cli.uninstall.spawn_helper", lambda *a, **k: True)
    fallback_calls: list[Path] = []
    monkeypatch.setattr(
        "cgate.cli.uninstall._spawn_delayed_delete",
        lambda target: fallback_calls.append(target) or True,
    )

    real_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == binary:
            raise PermissionError(13, "locked", str(self))
        if self == helper:
            raise PermissionError(13, "locked", str(self))
        real_unlink(self, *args, **kwargs)

    with patch.object(Path, "unlink", fake_unlink):
        result = runner.invoke(app, ["uninstall", "--binary", "--yes"])

    assert result.exit_code == 0, result.stdout
    assert fallback_calls == [helper]
    assert helper.exists()  # cleanup is deferred to the (mocked) shell-chain


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


def test_uninstall_data_aborts_rmtree_when_connection_enumeration_fails(
    runner: CliRunner, isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #40: when listing connections raises (DB locked, file unreadable,
    schema mismatch, ...), the data directory MUST stay where it is. Otherwise
    we destroy the only index that names the keyring entries for every
    connection, and those credentials are orphaned forever -- there is no
    later code path that can enumerate them.

    The fix routes the rmtree behind the same ``connection_enum_ok`` flag
    that gates the keyring cleanup, so a failure in either step leaves
    the data dir intact for a retry.
    """
    def list_all_raises(_self: object) -> list[Connection]:
        msg = "database is locked"
        raise ConnectionError(msg)

    monkeypatch.setattr(
        uninstall_module.ConnectionsRepo, "__init__", lambda _self, _db: None
    )
    monkeypatch.setattr(
        uninstall_module.ConnectionsRepo, "list_all", list_all_raises
    )

    # Sanity: the keyring cleanup must NOT be called either. If it runs,
    # it would silently succeed (the fake repo has no connections) and the
    # test would not be proving the abort.
    cleanup_called: list[str] = []

    def tracking_remove_credential(alias: str) -> None:
        cleanup_called.append(alias)

    monkeypatch.setattr(
        "cgate.cli.uninstall.remove_credential", tracking_remove_credential
    )

    runner.invoke(app, ["uninstall", "--data", "--yes"])

    # The data directory must survive.
    assert isolated_env["data"].exists(), (
        "issue #40: rmtree ran even though connection enumeration failed -- "
        "this orphans every connection's OS keyring credentials."
    )
    # The keyring cleanup must also be skipped (nothing to enumerate == nothing to remove).
    assert cleanup_called == []
    # (The user-facing message is the third leg of the contract -- without it,
    # the user would see 'data dir still here' with no explanation and assume
    # the uninstall silently aborted. The production code prints both the
    # refusal reason and the retry hint; checking the most specific phrase.)


def test_uninstall_data_message_appears_when_aborting_due_to_db_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to the above: confirm the user sees the explanation, not just
    a silent no-op. This is what makes the abort recoverable instead of a
    mystery -- the message tells them what to fix (close the lock-holder
    process) and that retrying is the right action.
    """
    def list_all_raises(_self: object) -> list[Connection]:
        msg = "database is locked"
        raise ConnectionError(msg)

    monkeypatch.setattr(
        uninstall_module.ConnectionsRepo, "__init__", lambda _self, _db: None
    )
    monkeypatch.setattr(uninstall_module.ConnectionsRepo, "list_all", list_all_raises)

    result = runner.invoke(app, ["uninstall", "--data", "--yes"])

    assert "Refusing to delete" in result.stdout
    assert "orphan" in result.stdout

"""Tests for cgate.cli.update helpers not covered by tests/test_update.py."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from cgate.cli.main import app
from cgate.cli.update import _spawn_delayed_swap
from cgate.update import Asset, Release

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_BLOCKING_PID = 999


def test_spawn_delayed_swap_suppresses_console_window_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cmd.exe helper must not flash a console window (issue #20)."""
    monkeypatch.setattr(sys, "platform", "win32")

    with patch("subprocess.Popen") as popen:
        ok = _spawn_delayed_swap("staging.exe", "cgate.exe")

    assert ok is True
    assert popen.call_args.kwargs["creationflags"] == _DETACHED_PROCESS | _CREATE_NO_WINDOW


def test_spawn_delayed_swap_uses_mv_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with patch("subprocess.Popen") as popen:
        ok = _spawn_delayed_swap("staging", "cgate")

    assert ok is True
    assert popen.call_args.args[0] == ["mv", "-f", "staging", "cgate"]
    assert "creationflags" not in popen.call_args.kwargs


def _mock_update_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub every collaborator so `update apply` reaches the blocker branch."""
    monkeypatch.setattr(
        "cgate.cli.update.fetch_latest_release",
        lambda: Release(tag="v9.9.9", version="9.9.9", html_url="https://x", assets=()),
    )
    monkeypatch.setattr("cgate.cli.update.__version__", "0.0.1")
    monkeypatch.setattr(
        "cgate.cli.update.select_asset",
        lambda _release: Asset("cgate-windows-amd64.exe", "https://x/bin", 1, ""),
    )
    monkeypatch.setattr("cgate.cli.update.current_binary_path", lambda: tmp_path / "cgate.exe")
    monkeypatch.setattr("cgate.cli.update.download_to", lambda _asset, _dest: None)
    monkeypatch.setattr(
        "cgate.cli.update.find_blocking_processes", lambda _binary: [_BLOCKING_PID]
    )


def test_apply_warns_and_asks_separately_before_killing_a_live_mcp_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #19: a blocker serving MCP must get its own loud, declined-by-default prompt."""
    _mock_update_available(monkeypatch, tmp_path)
    monkeypatch.setattr("cgate.cli.update.replace_binary", lambda _staging, _target: "locked")
    monkeypatch.setattr(
        "cgate.cli.update.find_mcp_serving_pids", lambda _pids: [_BLOCKING_PID]
    )
    with patch("cgate.cli.update.kill_process") as kill_process:
        result = CliRunner().invoke(app, ["update", "apply"], input="n\n")

    assert "live MCP session" in result.output
    assert f"PID(s) {_BLOCKING_PID}" in result.output
    kill_process.assert_not_called()


def test_apply_force_mcp_skips_the_mcp_specific_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    replace_calls = {"count": 0}

    def fake_replace_binary(_staging: Path, _target: Path) -> str | None:
        replace_calls["count"] += 1
        return "locked" if replace_calls["count"] == 1 else None

    _mock_update_available(monkeypatch, tmp_path)
    monkeypatch.setattr("cgate.cli.update.replace_binary", fake_replace_binary)
    monkeypatch.setattr(
        "cgate.cli.update.find_mcp_serving_pids", lambda _pids: [_BLOCKING_PID]
    )
    monkeypatch.setattr("cgate.cli.update.kill_process", lambda _pid: True)
    monkeypatch.setattr("cgate.cli.update.time.sleep", lambda _seconds: None)

    result = CliRunner().invoke(app, ["update", "apply", "--force-mcp"])

    assert result.exit_code == 0
    assert "Installed cgate 9.9.9" in result.output

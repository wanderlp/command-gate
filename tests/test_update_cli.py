"""Tests for cgate.cli.update helpers not covered by tests/test_update.py."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

from typer.testing import CliRunner

from cgate.cli.main import app
from cgate.cli.update import _spawn_delayed_swap, _swap_via_helper_or_fallback
from cgate.update import Asset, Release

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_BLOCKING_PID = 999
_MANUAL_RECOVERY_EXIT_CODE = 4
_UNCAUGHT_SWAP_FAILURE_MSG = "unexpected deep failure"


def test_spawn_delayed_swap_suppresses_console_window_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cmd.exe helper must not flash a console window (issue #20)."""
    monkeypatch.setattr(sys, "platform", "win32")

    with patch("subprocess.Popen") as popen:
        ok = _spawn_delayed_swap("staging.exe", "cgate.exe")

    assert ok is True
    assert popen.call_args.kwargs["creationflags"] == _DETACHED_PROCESS | _CREATE_NO_WINDOW


def test_spawn_delayed_swap_quotes_paths_with_spaces_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #44: a path containing a space (e.g. ``C:\\Program Files\\cgate``,
    which is the actual default Windows install location) used to be
    passed unquoted into the ``cmd.exe /c`` string -- cmd.exe would split
    on the space and try to execute ``C:\\Program`` as a command. The
    fix routes the path arguments through ``subprocess.list2cmdline`` so
    the resulting command line keeps the path as one argument.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    staging = r"C:\Program Files\cgate\cgate.exe.new"
    target = r"C:\Program Files\cgate\cgate.exe"

    with patch("subprocess.Popen") as popen:
        ok = _spawn_delayed_swap(staging, target)

    assert ok is True
    cmd_line = popen.call_args.args[0]
    # The path with a space must appear inside a quoted region, not raw.
    assert '"C:\\Program Files\\cgate\\cgate.exe.new"' in cmd_line
    assert '"C:\\Program Files\\cgate\\cgate.exe"' in cmd_line


def test_spawn_delayed_swap_quotes_paths_with_embedded_double_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #44: a path containing a literal ``"`` character would
    previously terminate the surrounding ``cmd.exe /c "..."`` quoted
    region early and expose the rest of the path as a separate token
    (which, combined with embedded ``&`` or ``|``, would let an extra
    command slip in). ``list2cmdline`` escapes embedded quotes as ``\\"``
    so the surrounding region stays intact and cmd.exe sees the whole
    path as one argument.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    staging = r'C:\evil"staging.exe'
    target = r'C:\target dir\evil"target.exe'

    with patch("subprocess.Popen") as popen:
        ok = _spawn_delayed_swap(staging, target)

    assert ok is True
    cmd_line = popen.call_args.args[0]
    # The embedded " must be escaped as \\", not left raw -- a raw " would
    # close the surrounding quoted region early.
    assert r'C:\evil\"staging.exe' in cmd_line
    assert r'C:\target dir\evil\"target.exe' in cmd_line


def test_spawn_delayed_swap_uses_mv_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with patch("subprocess.Popen") as popen:
        ok = _spawn_delayed_swap("staging", "cgate")

    assert ok is True
    assert popen.call_args.args[0] == ["mv", "-f", "staging", "cgate"]
    assert "creationflags" not in popen.call_args.kwargs


def test_swap_via_helper_prefers_the_compiled_helper_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "cgate.exe"
    staging = tmp_path / "cgate.exe.new"
    helper = tmp_path / "cgate-helper.exe"
    release = Release(tag="v9.9.9", version="9.9.9", html_url="https://x", assets=())

    # append_log resolves data_dir() at call time -- without this, a
    # successful (mocked) spawn writes a real line to the machine's
    # actual update.log instead of staying inside tmp_path.
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr("cgate.cli.update.ensure_helper_binary", lambda _b, _r: helper)
    spawn_calls: list[tuple] = []
    monkeypatch.setattr(
        "cgate.cli.update.spawn_helper",
        lambda _helper, *args: spawn_calls.append(args) or True,
    )
    with patch("cgate.cli.update._spawn_delayed_swap") as fallback:
        ok = _swap_via_helper_or_fallback(staging, binary, wait_pids=[4242], release=release)

    assert ok is True
    fallback.assert_not_called()
    assert spawn_calls == [
        ("replace", "--target", str(binary), "--source", str(staging), "--wait-pid", "4242")
    ]


def test_swap_via_helper_falls_back_when_helper_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "cgate.exe"
    staging = tmp_path / "cgate.exe.new"
    release = Release(tag="v9.9.9", version="9.9.9", html_url="https://x", assets=())

    monkeypatch.setattr("cgate.cli.update.ensure_helper_binary", lambda _b, _r: None)
    with patch("cgate.cli.update._spawn_delayed_swap", return_value=True) as fallback:
        ok = _swap_via_helper_or_fallback(staging, binary, wait_pids=[1], release=release)

    assert ok is True
    fallback.assert_called_once_with(staging, binary)


def test_swap_via_helper_falls_back_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "cgate.exe"
    staging = tmp_path / "cgate.exe.new"
    helper = tmp_path / "cgate-helper.exe"
    release = Release(tag="v9.9.9", version="9.9.9", html_url="https://x", assets=())

    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path)
    monkeypatch.setattr("cgate.cli.update.ensure_helper_binary", lambda _b, _r: helper)
    monkeypatch.setattr("cgate.cli.update.spawn_helper", lambda *_a, **_k: False)
    with patch("cgate.cli.update._spawn_delayed_swap", return_value=True) as fallback:
        ok = _swap_via_helper_or_fallback(staging, binary, wait_pids=[1], release=release)

    assert ok is True
    fallback.assert_called_once_with(staging, binary)


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
    # issue #4's attestation check is exercised separately in
    # tests/test_update.py; these tests care about the blocker/MCP branching
    # that runs after it, so treat it as already-verified.
    monkeypatch.setattr("cgate.cli.update.verify_attestation", lambda _asset, _release: None)
    # Accept the new ``exclude_pid`` kwarg added by issue #19's fix.
    monkeypatch.setattr(
        "cgate.cli.update.find_blocking_processes",
        lambda _binary, *, exclude_pid=None: [_BLOCKING_PID],  # noqa: ARG005
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


def test_apply_cleans_up_previous_snapshot_when_swap_never_happens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #15: no swap ever happened on this path, so the `.previous`
    snapshot taken from the still-current binary is just disk clutter --
    unlike `.new`, which the manual-recovery message still points at."""
    binary = tmp_path / "cgate.exe"
    binary.write_bytes(b"current binary bytes")
    monkeypatch.setattr(
        "cgate.cli.update.fetch_latest_release",
        lambda: Release(tag="v9.9.9", version="9.9.9", html_url="https://x", assets=()),
    )
    monkeypatch.setattr("cgate.cli.update.__version__", "0.0.1")
    monkeypatch.setattr(
        "cgate.cli.update.select_asset",
        lambda _release: Asset("cgate-windows-amd64.exe", "https://x/bin", 1, ""),
    )
    monkeypatch.setattr("cgate.cli.update.current_binary_path", lambda: binary)
    monkeypatch.setattr("cgate.cli.update.download_to", lambda _asset, _dest: None)
    monkeypatch.setattr("cgate.cli.update.verify_attestation", lambda _asset, _release: None)
    monkeypatch.setattr("cgate.cli.update.replace_binary", lambda _staging, _target: "locked")
    # issue #19: find_blocking_processes now takes ``exclude_pid``.
    monkeypatch.setattr(
        "cgate.cli.update.find_blocking_processes",
        lambda _binary, *, exclude_pid=None: [_BLOCKING_PID],  # noqa: ARG005
    )
    monkeypatch.setattr("cgate.cli.update.find_mcp_serving_pids", lambda _pids: [])

    result = CliRunner().invoke(app, ["update", "apply"], input="n\n")

    previous = binary.with_name(binary.name + ".previous")
    staging = binary.with_name(binary.name + ".new")
    # Rich wraps long lines for the console width, so compare with
    # newlines collapsed rather than assuming the path prints unbroken.
    unwrapped_output = result.output.replace("\n", "")
    assert result.exit_code == _MANUAL_RECOVERY_EXIT_CODE
    assert not previous.exists()
    assert "Rollback slot" not in unwrapped_output
    assert "Staged download" in unwrapped_output
    assert str(staging) in unwrapped_output


# --- Reopen follow-ups ---


def test_apply_warns_loudly_when_running_process_serves_mcp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #19 (reopened): self-lock alone used to skip the MCP
    warning because `find_blocking_processes` always reported the
    running process as its own blocker (no `exclude_pid`). The fix
    must surface the warning even when the swap-failure cause is
    self-lock and the running process is the MCP server."""
    _mock_update_available(monkeypatch, tmp_path)
    self_pid = os.getpid()
    monkeypatch.setattr(
        "cgate.cli.update.find_blocking_processes",
        lambda _binary, *, exclude_pid=None: [],  # noqa: ARG005
    )
    monkeypatch.setattr(
        "cgate.cli.update.find_mcp_serving_pids",
        lambda pids: [self_pid] if self_pid in pids else [],
    )
    monkeypatch.setattr("cgate.cli.update.replace_binary", lambda _s, _t: "locked")
    monkeypatch.setattr(
        "cgate.cli.update._spawn_delayed_swap", lambda _s, _t: True
    )

    result = CliRunner().invoke(app, ["update", "apply"], input="y\n")

    assert "live MCP session" in result.output
    assert str(self_pid) in result.output
    assert "Update staged" in result.output
    assert result.exit_code == 0


def test_apply_force_mcp_required_for_self_locked_mcp_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #19 (reopened): --force-mcp is the bypass for the loud
    self-MCP warning. Without it, declining the prompt must NOT spawn
    the delayed-swap helper -- only the manual-recovery path runs."""
    _mock_update_available(monkeypatch, tmp_path)
    self_pid = os.getpid()
    monkeypatch.setattr(
        "cgate.cli.update.find_blocking_processes",
        lambda _binary, *, exclude_pid=None: [],  # noqa: ARG005
    )
    monkeypatch.setattr(
        "cgate.cli.update.find_mcp_serving_pids",
        lambda pids: [self_pid] if self_pid in pids else [],
    )
    monkeypatch.setattr("cgate.cli.update.replace_binary", lambda _s, _t: "locked")
    spawn_calls = {"count": 0}
    monkeypatch.setattr(
        "cgate.cli.update._spawn_delayed_swap",
        lambda _s, _t: (spawn_calls.update(count=spawn_calls["count"] + 1) or True),
    )

    result = CliRunner().invoke(app, ["update", "apply"], input="n\n")

    assert "live MCP session" in result.output
    assert spawn_calls["count"] == 0


def test_apply_orphans_are_flagged_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #15 (reopened): `.new` / `.previous` files left over from
    a previous run must be surfaced to the user instead of silently
    overwritten."""
    binary = tmp_path / "cgate.exe"
    binary.write_bytes(b"current binary bytes")
    staging = binary.with_name(binary.name + ".new")
    previous = binary.with_name(binary.name + ".previous")
    staging.write_bytes(b"staged from last run")
    previous.write_bytes(b"snapshot from last run")

    monkeypatch.setattr(
        "cgate.cli.update.fetch_latest_release",
        lambda: Release(tag="v9.9.9", version="9.9.9", html_url="https://x", assets=()),
    )
    monkeypatch.setattr("cgate.cli.update.__version__", "9.9.9")
    # Short-circuit before any overwrite happens so we can read the
    # warning without first nuking the orphans.
    monkeypatch.setattr("cgate.cli.update.compare_versions", lambda _a, _b: 0)
    monkeypatch.setattr("cgate.cli.update.current_binary_path", lambda: binary)

    result = CliRunner().invoke(app, ["update", "apply"])

    # Rich wraps long paths across newlines, so compare against an
    # unwrapped copy and the original to cover both.
    unwrapped = result.output.replace("\n", "")
    assert "leftover staged download" in unwrapped.lower()
    assert "leftover rollback snapshot" in unwrapped.lower()
    assert str(staging) in unwrapped
    assert str(previous) in unwrapped
    assert staging.exists()
    assert previous.exists()


def test_apply_cleans_up_staged_files_on_uncaught_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #15 (reopened): an unexpected exception between successful
    download and end of swap handling must not leak `.new` / `.previous`."""
    binary = tmp_path / "cgate.exe"
    binary.write_bytes(b"current binary bytes")
    monkeypatch.setattr(
        "cgate.cli.update.fetch_latest_release",
        lambda: Release(tag="v9.9.9", version="9.9.9", html_url="https://x", assets=()),
    )
    monkeypatch.setattr("cgate.cli.update.__version__", "0.0.1")
    monkeypatch.setattr(
        "cgate.cli.update.select_asset",
        lambda _release: Asset("cgate-windows-amd64.exe", "https://x/bin", 1, ""),
    )
    monkeypatch.setattr("cgate.cli.update.current_binary_path", lambda: binary)
    monkeypatch.setattr("cgate.cli.update.download_to", lambda _a, _d: None)
    monkeypatch.setattr("cgate.cli.update.verify_attestation", lambda _asset, _release: None)

    def _boom(_staging: Path, _target: Path) -> str | None:
        raise RuntimeError(_UNCAUGHT_SWAP_FAILURE_MSG)

    monkeypatch.setattr("cgate.cli.update.replace_binary", _boom)

    result = CliRunner().invoke(app, ["update", "apply"])

    previous = binary.with_name(binary.name + ".previous")
    staging = binary.with_name(binary.name + ".new")
    assert isinstance(result.exception, RuntimeError)
    assert not staging.exists()
    assert not previous.exists()

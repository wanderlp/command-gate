"""Tests for cgate.cli.update helpers not covered by tests/test_update.py."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

from cgate.cli.update import _spawn_delayed_swap

if TYPE_CHECKING:
    import pytest

_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000


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

"""Tests for the top-level `main()` entry point (issue #12)."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from cgate.cli.main import main


def test_main_converts_uncaught_sqlite_error_into_clean_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A locked/corrupted DB must not surface as a raw traceback."""
    with (
        patch("cgate.cli.main.app", side_effect=sqlite3.OperationalError("database is locked")),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1
    assert "database is locked" in capsys.readouterr().err


def test_main_does_not_swallow_other_exceptions() -> None:
    """Only sqlite3.Error is converted; anything else still propagates."""
    with (
        patch("cgate.cli.main.app", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        main()

"""Tests for ``cgate.cli._swap_helper.append_log``."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgate.cli._swap_helper import append_log


def test_append_log_writes_to_data_dir_update_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path / "data")
    append_log("first message")
    append_log("second message")
    log_path = tmp_path / "data" / "update.log"
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "first message" in contents
    assert "second message" in contents


def test_append_log_creates_parent_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path / "deep" / "nested")
    append_log("hello")
    assert (tmp_path / "deep" / "nested" / "update.log").exists()


def test_append_log_never_raises_when_data_dir_unwritable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logging must be best-effort; failure must not crash the caller."""

    def boom() -> Path:
        raise OSError(13, "permission denied")

    monkeypatch.setattr("cgate.core.paths.data_dir", boom)
    # Should NOT raise.
    append_log("unimportant")

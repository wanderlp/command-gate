"""Tests for ``cgate.core.update_log.append_log``."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgate.core.update_log import _MAX_LOG_BYTES, append_log


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


def test_append_log_does_not_rotate_below_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #43: rotation must not touch the file when it is below the cap.
    Writes of small lines never grow the file anywhere near the 1 MiB cap,
    so the file must be left untouched (no read-then-rewrite round-trip)
    and the original contents must be intact.
    """
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path / "data")
    for i in range(20):
        append_log(f"line {i}")
    log_path = tmp_path / "data" / "update.log"
    contents = log_path.read_text(encoding="utf-8")
    # All 20 lines present, in order.
    for i in range(20):
        assert f"line {i}" in contents
    # Well under the cap.
    assert log_path.stat().st_size < _MAX_LOG_BYTES


def test_append_log_rotates_when_exceeding_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #43: once the log exceeds the 1 MiB cap, the next append must
    trigger rotation -- oldest lines dropped, newest preserved, file size
    brought back under the cap with headroom.
    """
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path / "data")
    # Use a deterministic, easy-to-eyeball payload: 5000-byte messages.
    # ~250 of them is enough to cross the 1 MiB cap with room to spare.
    payload = "x" * 5_000
    for i in range(300):
        append_log(f"{i:04d}:{payload}")
    log_path = tmp_path / "data" / "update.log"
    final_size = log_path.stat().st_size
    # The file is now bounded well below the cap -- rotation leaves ~75%
    # of the cap behind so we do not rotate on every subsequent write.
    assert final_size < _MAX_LOG_BYTES
    assert final_size > 0
    contents = log_path.read_text(encoding="utf-8")
    # The most recent entry is preserved; the oldest entry, which would
    # only exist if rotation had failed, is not.
    assert "0299:" in contents
    assert "0000:" not in contents


def test_append_log_keeps_most_recent_content_after_rotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #43: rotation's contract is "most recent content survives".
    Verify by tagging a marker string into the last write before rotation
    and asserting it is still readable after the cap kicks in.
    """
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path / "data")
    payload = "y" * 5_000
    # Fill past the cap with anonymous lines.
    for i in range(300):
        append_log(f"{i:04d}:{payload}")
    # Append one more line with a unique marker that must survive rotation.
    append_log("MARKER: this line is the latest and must survive")
    log_path = tmp_path / "data" / "update.log"
    contents = log_path.read_text(encoding="utf-8")
    assert "MARKER:" in contents
    assert contents.rstrip().endswith("must survive")


def test_append_log_rotation_is_silent_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """issue #43 (best-effort contract): even when the rotation step
    itself fails, ``append_log`` must not raise. We simulate by patching
    ``_enforce_size_cap`` to raise -- the outer ``try/except`` in
    ``append_log`` catches it and the call returns normally.
    """
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path / "data")

    def boom(_log_path: Path) -> None:
        raise OSError(5, "input/output error")

    monkeypatch.setattr("cgate.core.update_log._enforce_size_cap", boom)
    # Must NOT raise even though the rotation step fails.
    append_log("anything")

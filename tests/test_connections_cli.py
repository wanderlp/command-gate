"""CLI tests for ``cgate connections add``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cgate.cli.main import app
from cgate.db.types import ServerType


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolated DB and data dir so tests never touch the real local store.

    ``data_dir`` is patched at ``cgate.cli.connections`` (not
    ``cgate.core.paths``) because cli/connections.py imports it at module
    level, so the import-name binding must be patched for the patch to
    take effect inside ``_db``.
    """
    monkeypatch.setenv("CGATE_DB_PATH", str(tmp_path / "cgate.db"))
    monkeypatch.setattr(
        "cgate.cli.connections.data_dir", lambda: tmp_path / "data"
    )
    return {"db": tmp_path / "cgate.db", "data": tmp_path / "data"}


@pytest.fixture
def fake_probe() -> MagicMock:
    """A probe result that resolves to LINUX/SSH without touching the network."""
    probe = MagicMock()
    probe.server_type = ServerType.LINUX
    probe.ssh = True
    probe.winrm = False
    return probe


def test_connections_add_reports_duplicate_alias_friendly(
    runner: CliRunner,
    isolated_env: dict[str, Path],
    fake_probe: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1: a duplicate alias must surface a friendly error, not a
    raw ``IntegrityError`` traceback."""
    # Kerberos short-circuits the credential prompts so the test stays
    # focused on the duplicate-alias path.
    monkeypatch.setattr("cgate.cli.connections.is_kerberos_available", lambda: True)

    with patch("cgate.cli.connections.probe_host", return_value=fake_probe):
        # First add: should succeed.
        first = runner.invoke(
            app,
            ["connections", "add", "srv-test", "example.com"],
        )
        assert first.exit_code == 0, first.stdout

        # Second add with the same alias: must fail with the friendly
        # message and a non-zero exit code.
        second = runner.invoke(
            app,
            ["connections", "add", "srv-test", "example.com"],
        )

    assert second.exit_code == 1, second.stdout
    assert "already exists" in second.stdout
    assert "srv-test" in second.stdout
    # Rich wraps the long command name across lines, so the substring
    # is literally "cgate connections\nremove srv-test" in stdout.
    assert "cgate connections\nremove srv-test" in second.stdout
    # The raw Python traceback must NOT leak to the user.
    assert "Traceback" not in second.stdout
    assert "IntegrityError" not in second.stdout


def test_connections_add_skips_network_probe_when_alias_exists(
    runner: CliRunner,
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #16: a duplicate alias must short-circuit BEFORE the
    network probe and credential prompts so the user does not waste
    time on a host that the alias collision has already invalidated.
    """
    monkeypatch.setattr("cgate.cli.connections.is_kerberos_available", lambda: True)

    probe_calls: list[str] = []

    def counting_probe(hostname: str):
        probe_calls.append(hostname)
        probe = MagicMock()
        probe.server_type = ServerType.LINUX
        probe.ssh = True
        probe.winrm = False
        return probe

    with patch("cgate.cli.connections.probe_host", side_effect=counting_probe):
        # First add seeds the DB with this alias.
        first = runner.invoke(
            app,
            ["connections", "add", "srv-test", "first.example.com"],
        )
        assert first.exit_code == 0, first.stdout
        assert probe_calls == ["first.example.com"]

        # Second add with the same alias must NOT call probe_host at all
        # -- the user should not pay for a network round-trip + prompts
        # for an alias that is already taken.
        probe_calls.clear()
        second = runner.invoke(
            app,
            ["connections", "add", "srv-test", "second.example.com"],
        )

    assert second.exit_code == 1
    assert probe_calls == [], (
        f"probe_host should not be called for a duplicate alias; "
        f"got calls for: {probe_calls!r}"
    )
    assert "already exists" in second.stdout


def test_connections_add_succeeds_for_new_alias(
    runner: CliRunner,
    isolated_env: dict[str, Path],
    fake_probe: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: a fresh alias inserts cleanly and exits 0."""
    monkeypatch.setattr("cgate.cli.connections.is_kerberos_available", lambda: True)
    with patch("cgate.cli.connections.probe_host", return_value=fake_probe):
        result = runner.invoke(
            app,
            ["connections", "add", "srv-fresh", "fresh.example.com"],
        )
    assert result.exit_code == 0, result.stdout
    assert "Saved" in result.stdout
    assert "srv-fresh" in result.stdout

"""CLI tests for ``cgate connections add`` and ``cgate connections remove``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from typer.testing import CliRunner

from cgate.cli.main import app
from cgate.db.types import ServerType

if TYPE_CHECKING:
    from collections.abc import Generator


class _FakeKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        key = (service, username)
        if key not in self.store:
            raise PasswordDeleteError
        del self.store[key]


@pytest.fixture
def fake_keyring() -> Generator[_FakeKeyring, None, None]:
    """Isolate keyring I/O so tests never touch the real OS credential store."""
    original = keyring.get_keyring()
    fake = _FakeKeyring()
    keyring.set_keyring(fake)
    yield fake
    keyring.set_keyring(original)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolated DB so tests never touch the real local store.

    Issue #9: ``cli/connections.py`` now builds its DB path via
    ``db_path()``, which honors ``CGATE_DB_PATH`` -- setting the env var
    is enough for isolation, no module-level patch needed.
    """
    monkeypatch.setenv("CGATE_DB_PATH", str(tmp_path / "cgate.db"))
    return {"db": tmp_path / "cgate.db"}


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


def test_connections_remove_happy_path(
    runner: CliRunner,
    isolated_env: dict[str, Path],
    fake_probe: MagicMock,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cgate.cli.connections.is_kerberos_available", lambda: True)
    with patch("cgate.cli.connections.probe_host", return_value=fake_probe):
        added = runner.invoke(app, ["connections", "add", "srv-test", "example.com"])
    assert added.exit_code == 0, added.stdout

    result = runner.invoke(app, ["connections", "remove", "srv-test"], input="y\n")

    assert result.exit_code == 0, result.stdout
    assert "Removed" in result.stdout
    listing = runner.invoke(app, ["connections", "list"])
    assert "srv-test" not in listing.stdout


def test_connections_remove_reports_missing_alias(
    runner: CliRunner, isolated_env: dict[str, Path]
) -> None:
    result = runner.invoke(app, ["connections", "remove", "does-not-exist"], input="y\n")
    assert result.exit_code == 1
    assert "No connection" in result.stdout


def test_connections_remove_aborts_before_deleting_row_when_keyring_fails(
    runner: CliRunner,
    isolated_env: dict[str, Path],
    fake_probe: MagicMock,
    fake_keyring: _FakeKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #5: if the keyring backend fails, the DB row must survive --
    deleting it first would leave a stray, unreachable keyring credential."""
    monkeypatch.setattr("cgate.cli.connections.is_kerberos_available", lambda: True)
    with patch("cgate.cli.connections.probe_host", return_value=fake_probe):
        added = runner.invoke(app, ["connections", "add", "srv-test", "example.com"])
    assert added.exit_code == 0, added.stdout

    with patch(
        "cgate.cli.connections.remove_credential",
        side_effect=RuntimeError("keyring backend unavailable"),
    ):
        result = runner.invoke(app, ["connections", "remove", "srv-test"], input="y\n")

    assert result.exit_code == 1
    assert "NOT removed" in result.stdout
    listing = runner.invoke(app, ["connections", "list"])
    assert "srv-test" in listing.stdout

from __future__ import annotations

import contextlib
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from typer.testing import CliRunner

from cgate.cli.main import app
from cgate.connections.auth import (
    KEYRING_SERVICE,
    get_credential,
    is_kerberos_available,
    remove_credential,
    store_credential,
)
from cgate.connections.detect import (
    AmbiguousHostError,
    DetectionProbe,
    UnknownHostError,
    probe_host,
)
from cgate.connections.store import ConnectionsRepo
from cgate.db.connection import Database, connect, init_database
from cgate.db.schema import SCHEMA_VERSION
from cgate.db.types import ServerType

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

EXPECTED_SCHEMA_VERSION = 2


class FakeKeyring(KeyringBackend):
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
def fake_keyring() -> Generator[FakeKeyring, None, None]:
    original = keyring.get_keyring()
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    yield fake
    keyring.set_keyring(original)


def _db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    return db


def _probe_with_open_ports(hostname: str, open_ports: set[int]) -> DetectionProbe:
    def create_connection(
        address: tuple[str, int], *, timeout: float
    ) -> contextlib.AbstractContextManager[None]:
        del timeout
        if address[1] not in open_ports:
            raise OSError
        return contextlib.nullcontext()

    with patch("socket.create_connection", side_effect=create_connection):
        return probe_host(hostname)


def test_probe_host_detects_ssh_only() -> None:
    probe = _probe_with_open_ports("linux.example", {22})
    assert probe.ssh is True
    assert probe.winrm is False
    assert probe.server_type is ServerType.LINUX


def test_probe_host_detects_winrm_only() -> None:
    probe = _probe_with_open_ports("windows.example", {5986})
    assert probe.ssh is False
    assert probe.winrm is True
    assert probe.server_type is ServerType.WINDOWS


def test_probe_host_reports_ambiguous_host() -> None:
    probe = _probe_with_open_ports("dual.example", {22, 5985})
    with pytest.raises(AmbiguousHostError):
        _ = probe.server_type


def test_probe_host_reports_unknown_host() -> None:
    probe = _probe_with_open_ports("offline.example", set())
    with pytest.raises(UnknownHostError):
        _ = probe.server_type


def test_detection_probe_raises_for_ambiguous_result() -> None:
    probe = DetectionProbe(hostname="dual.example", ssh=True, winrm=True)
    with pytest.raises(AmbiguousHostError):
        _ = probe.server_type


def test_detection_probe_raises_for_unknown_result() -> None:
    probe = DetectionProbe(hostname="offline.example", ssh=False, winrm=False)
    with pytest.raises(UnknownHostError):
        _ = probe.server_type


def test_connections_repo_roundtrip_and_alias_order(tmp_path: Path) -> None:
    repo = ConnectionsRepo(_db(tmp_path))
    second = repo.add(
        alias="z-linux",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    first = repo.add(
        alias="a-windows",
        hostname="windows.example",
        server_type=ServerType.WINDOWS,
        detection_ssh=False,
        detection_winrm=True,
    )
    assert repo.get(second.alias) == second
    assert repo.list_all() == [first, second]


def test_connections_repo_remove_existing_returns_true(tmp_path: Path) -> None:
    repo = ConnectionsRepo(_db(tmp_path))
    connection = repo.add(
        alias="server",
        hostname="server.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    assert repo.remove(connection.alias) is True
    assert repo.get(connection.alias) is None


def test_connections_repo_remove_missing_returns_false(tmp_path: Path) -> None:
    repo = ConnectionsRepo(_db(tmp_path))
    assert repo.remove("missing") is False


def test_schema_v2_is_recorded_idempotently(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_database(db)
    with connect(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_version WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
    assert SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION
    assert row is not None
    assert row["n"] == 1


def test_credential_roundtrip_and_removal(fake_keyring: FakeKeyring) -> None:
    stored_value = f"test-value-{len(fake_keyring.store)}"
    store_credential(
        "server", username="operator", password=stored_value, ssh_key=None
    )
    credential = get_credential("server")
    assert credential is not None
    assert credential.username == "operator"
    assert credential.password == stored_value
    assert credential.ssh_key is None
    assert (KEYRING_SERVICE, "connection:server") in fake_keyring.store
    assert remove_credential("server") is True
    assert get_credential("server") is None


def test_remove_missing_credential_returns_false(fake_keyring: FakeKeyring) -> None:
    assert remove_credential("missing") is False
    assert fake_keyring.store == {}


def test_kerberos_is_unavailable_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert is_kerberos_available() is False


def test_cli_list_on_empty_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CGATE_DB_PATH", str(tmp_path / "cgate.db"))
    monkeypatch.setattr("cgate.cli.connections.data_dir", lambda: tmp_path)
    result = CliRunner().invoke(app, ["connections", "list"])
    assert result.exit_code == 0
    assert "No connections" in result.output


def test_cli_add_help_shows_usage() -> None:
    result = CliRunner().invoke(app, ["connections", "add", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output

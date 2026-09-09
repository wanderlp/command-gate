from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from cgate.mcp_installer import (
    ClientInstall,
    current_binary_command,
    detect_clients,
    is_registered,
    read_json,
    register,
    unregister,
    write_json_atomic,
)


def _client(path: Path) -> ClientInstall:
    return ClientInstall("claude", "Claude Code", path)


def test_detect_clients_finds_existing_configs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / ".claude" / "mcp.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert detect_clients() == [_client(config)]


def test_detect_clients_returns_empty_when_none_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert detect_clients() == []


def test_read_json_returns_empty_dict_on_missing_file(tmp_path: Path) -> None:
    assert read_json(tmp_path / "missing.json") == {}


def test_read_json_returns_empty_dict_on_invalid_json(tmp_path: Path) -> None:
    config = tmp_path / "invalid.json"
    config.write_text("not-json", encoding="utf-8")

    assert read_json(config) == {}


def test_write_json_atomic_creates_backup_file(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text('{"before": true}\n', encoding="utf-8")

    write_json_atomic(config, {"after": True})

    assert config.with_suffix(".json.bak").read_text(encoding="utf-8") == (
        '{"before": true}\n'
    )


def test_register_adds_cgate_to_existing_claude_config(tmp_path: Path) -> None:
    config = tmp_path / ".claude" / "mcp.json"
    config.parent.mkdir()
    config.write_text('{"theme": "dark"}', encoding="utf-8")

    register(_client(config), "cgate", ["mcp", "serve"])

    assert json.loads(config.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "mcpServers": {
            "cgate": {"command": "cgate", "args": ["mcp", "serve"]}
        },
    }


def test_register_is_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    client = _client(config)

    register(client, "cgate", ["mcp", "serve"])
    register(client, "cgate", ["mcp", "serve"])

    servers = read_json(config)["mcpServers"]
    assert list(servers) == ["cgate"]


def test_unregister_removes_cgate_entry(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    client = _client(config)
    register(client, "cgate", ["mcp", "serve"])

    assert unregister(client) is True
    assert is_registered(client) is False


def test_unregister_returns_false_when_absent(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers": {}}', encoding="utf-8")

    assert unregister(_client(config)) is False


def test_is_registered_true_when_present(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers": {"cgate": {"command": "cgate", "args": []}}}',
        encoding="utf-8",
    )

    assert is_registered(_client(config)) is True


def test_current_binary_command_returns_exe_path_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = "C:\\fake\\cgate.exe"
    monkeypatch.setattr(sys, "executable", executable)

    command, args = current_binary_command()

    assert Path(command) == Path(executable).resolve()
    assert args == ["mcp", "serve"]

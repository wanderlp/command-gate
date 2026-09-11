from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    pass

from cgate.cli.main import app
from cgate.mcp_installer import (
    CLIENTS,
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
    """Regression for issue #2: Claude Code's actual config path is
    ``~/.claude.json``, NOT ``~/.claude/mcp.json``."""
    config = tmp_path / ".claude.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert detect_clients() == [_client(config)]


def test_detect_clients_finds_claude_via_settings_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alternative Claude Code location: ``~/.claude/settings.json``."""
    config = tmp_path / ".claude" / "settings.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert detect_clients() == [_client(config)]


def test_detect_clients_prefers_first_path_when_multiple_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When BOTH Claude Code paths exist, the first one listed in
    CLIENTS wins so register/unregister write back to the same file
    that was detected."""
    primary = tmp_path / ".claude.json"
    secondary = tmp_path / ".claude" / "settings.json"
    secondary.parent.mkdir(parents=True)
    primary.write_text("{}", encoding="utf-8")
    secondary.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert detect_clients() == [_client(primary)]


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


@pytest.mark.parametrize("name", list(CLIENTS))
def test_register_and_unregister_roundtrip_for_every_supported_client(
    name: str, tmp_path: Path
) -> None:
    """issue #18: every entry in CLIENTS must work end-to-end on its own --
    a client added there with an incomplete/wrong spec should fail here,
    not as a distant KeyError."""
    spec = CLIENTS[name]
    client = ClientInstall(name=name, label=spec.label, config_path=tmp_path / "config.json")

    register(client, "cgate", ["mcp", "serve"])
    assert is_registered(client) is True

    assert unregister(client) is True
    assert is_registered(client) is False


def test_current_binary_command_returns_exe_path_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = "C:\\fake\\cgate.exe"
    monkeypatch.setattr(sys, "executable", executable)

    command, args = current_binary_command()

    assert Path(command) == Path(executable).resolve()
    assert args == ["mcp", "serve"]


def test_install_cmd_shows_one_status_line_per_detected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for issue #3: each detected client must get its own
    status line in the ``Detected N IA client(s)`` block. Before the
    fix the print was indented outside the ``for`` loop, so only the
    LAST client was printed (and only once)."""
    claude = ClientInstall(
        name="claude", label="Claude Code", config_path=Path("/fake/.claude.json")
    )
    opencode = ClientInstall(
        name="opencode",
        label="opencode",
        config_path=Path("/fake/.config/opencode/opencode.jsonc"),
    )
    monkeypatch.setattr("cgate.cli.mcp.detect_clients", lambda: [claude, opencode])

    def fake_is_registered(client: ClientInstall) -> bool:
        return client.name == "claude"

    monkeypatch.setattr("cgate.cli.mcp.is_registered", fake_is_registered)

    runner = CliRunner()
    result = runner.invoke(app, ["mcp", "install", "--dry-run", "--yes"])

    assert result.exit_code == 0, result.stdout

    # Filter to the per-client status lines (the "  - <label> ..." lines).
    status_lines = [
        line
        for line in result.stdout.splitlines()
        if "  - Claude Code" in line or "  - opencode" in line
    ]
    assert len(status_lines) == 2, (
        f"Expected exactly 2 status lines (one per client); got "
        f"{len(status_lines)}: {status_lines!r}"
    )

    claude_line = next(line for line in status_lines if "Claude Code" in line)
    assert "registered" in claude_line
    assert "not registered" not in claude_line

    opencode_line = next(line for line in status_lines if "opencode" in line)
    assert "not registered" in opencode_line

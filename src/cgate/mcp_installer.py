"""Detect IA clients and register cgate's MCP server."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ClientSpec:
    """Everything cgate needs to know about one supported IA client.

    Kept as a single record per client (issue #18) so adding a client is
    one atomic edit to ``CLIENTS`` instead of three parallel dicts that
    can silently drift out of sync and surface as a ``KeyError`` far from
    the point where a client was actually added.
    """

    label: str
    # Multiple paths because some clients (notably Claude Code) store MCP
    # config in more than one location, ordered by precedence: when a
    # client is detected, the FIRST existing path is used as the primary
    # config_path for register/unregister/is_registered. Paths are home-
    # relative and joined with Path.home() at detection time.
    config_paths: tuple[str, ...]
    config_key: str


CLIENTS: Final[dict[str, ClientSpec]] = {
    "claude": ClientSpec(
        label="Claude Code",
        # Claude Code does NOT use ``~/.claude/mcp.json`` -- it stores MCP
        # servers in ``~/.claude.json`` (legacy global) or
        # ``~/.claude/settings.json`` (project-level global). The first
        # existing one wins. See issue #2.
        config_paths=(".claude.json", ".claude/settings.json"),
        config_key="mcpServers",
    ),
    "opencode": ClientSpec(
        label="opencode",
        config_paths=(".config/opencode/opencode.jsonc",),
        config_key="mcp",
    ),
    "cursor": ClientSpec(
        label="Cursor",
        config_paths=(".cursor/mcp.json",),
        config_key="mcpServers",
    ),
}
CONFIG_SERVER_ENTRY_KEY: Final = "cgate"


@dataclass(frozen=True, slots=True)
class ClientInstall:
    """One installed IA client and its config file path."""

    name: str
    label: str
    config_path: Path


def detect_clients() -> list[ClientInstall]:
    """Return IA clients whose config file exists under the user's home.

    A client is considered "installed" if ANY of the paths listed for it
    in ``CLIENTS`` exists. When several paths exist (Claude Code can have
    both ``~/.claude.json`` and ``~/.claude/settings.json``), the FIRST
    existing path is returned so register/unregister write back to the
    same file that was detected.
    """
    home = Path.home()
    found: list[ClientInstall] = []
    for name, spec in CLIENTS.items():
        for relative in spec.config_paths:
            candidate = home / relative
            if candidate.exists():
                found.append(ClientInstall(name=name, label=spec.label, config_path=candidate))
                break
    return found


def read_json(path: Path) -> JsonObject:
    """Read a JSON config, returning an empty config when missing or invalid."""
    try:
        with path.open(encoding="utf-8") as stream:
            data: JsonObject = json.load(stream)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data


def write_json_atomic(path: Path, data: JsonObject) -> None:
    """Back up a config, then durably write and atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        if path.exists():
            _ = shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            _ = stream.write("\n")
            stream.flush()
            _ = os.fsync(stream.fileno())
        _ = temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def current_binary_command() -> tuple[str, list[str]]:
    """Return the command and arguments clients use to launch cgate's MCP server."""
    executable = Path(sys.executable).resolve()
    if executable.name.startswith("cgate"):
        return str(executable), ["mcp", "serve"]
    return sys.executable, ["-m", "cgate.mcp_server"]


def register(client: ClientInstall, command: str, args: list[str]) -> None:
    """Add or update the client's cgate entry idempotently."""
    config = read_json(client.config_path)
    config_key = CLIENTS[client.name].config_key
    bucket_value = config.get(config_key)
    bucket: JsonObject = bucket_value if isinstance(bucket_value, dict) else {}
    entry_args: list[JsonValue] = [*args]
    if client.name == "opencode":
        entry: JsonObject = {
            "type": "local",
            "command": [command, *entry_args],
        }
    else:
        entry = {"command": command, "args": entry_args}
    bucket[CONFIG_SERVER_ENTRY_KEY] = entry
    config[config_key] = bucket
    write_json_atomic(client.config_path, config)


def unregister(client: ClientInstall) -> bool:
    """Remove cgate from a client's config and report whether it was present."""
    config = read_json(client.config_path)
    config_key = CLIENTS[client.name].config_key
    bucket_value = config.get(config_key)
    if not isinstance(bucket_value, dict) or CONFIG_SERVER_ENTRY_KEY not in bucket_value:
        return False
    del bucket_value[CONFIG_SERVER_ENTRY_KEY]
    write_json_atomic(client.config_path, config)
    return True


def is_registered(client: ClientInstall) -> bool:
    """Return whether the client's config contains a cgate MCP entry."""
    bucket = read_json(client.config_path).get(CLIENTS[client.name].config_key)
    return isinstance(bucket, dict) and CONFIG_SERVER_ENTRY_KEY in bucket

"""MCP server factory exposing command-gate tools over the stdio transport."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

import anyio
import mcp.server.stdio
from mcp import types
from mcp.server import Server, ServerRequestContext

from cgate import __version__
from cgate.connections.store import ConnectionsRepo
from cgate.core.paths import data_dir
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, init_database
from cgate.db.mode import AppModeRepo
from cgate.db.server_settings import ServerSettingsRepo
from cgate.mcp_server.tools import (
    BatchStatusResult,
    ConnectionResult,
    ModeResult,
    ProposeCommandResult,
    ToolError,
    check_status,
    get_mode,
    list_connections,
    propose_command,
)
from cgate.update import maybe_heal_pending_update

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

ToolPayload: TypeAlias = (
    ProposeCommandResult
    | BatchStatusResult
    | ModeResult
    | list[ConnectionResult]
    | dict[str, str]
)

_SERVER_INSTRUCTIONS = (
    "cgate lets you run commands on servers gated behind a human approval "
    "queue -- or, for servers explicitly opted into AUTO mode, executed "
    "immediately. Typical flow: call list_connections to see which server "
    "aliases exist and each one's server_type (the dialect your command "
    "must be written in), then get_mode to see whether the target server "
    "executes immediately or queues for a human. Call propose_command to "
    'register a command. If the response comes back status: "pending", '
    "nothing has run yet -- call check_status(batch_id) later to learn "
    "whether a human approved it, rejected it, or it finished executing "
    'and what the result was. If it comes back status: "executed" or '
    '"failed", it already ran under AUTO mode and result holds the '
    "output. Match command syntax to the target's server_type: WINDOWS "
    "commands run through a real PowerShell session (run_ps) -- write "
    "idiomatic PowerShell, not legacy cmd.exe batch syntax, keep "
    "Windows's ~8KB command-line length limit in mind, and avoid "
    "cramming multi-step logic with nested quotes/brackets into one "
    "dense pipeline (PowerShell's quoting of long single-line pipelines "
    "is fragile). LINUX commands run through a POSIX shell over SSH. A "
    "command flagged risky_command always queues for a human regardless "
    "of mode -- but the absence of that flag is not a safety guarantee, "
    "only a heuristic over a known set of destructive patterns."
)


@dataclass(frozen=True, slots=True)
class _ToolDeps:
    """Repositories opened independently for one MCP tool invocation."""

    db: Database
    batches: BatchesRepo
    commands: CommandsRepo
    connections: ConnectionsRepo
    mode: AppModeRepo
    settings: ServerSettingsRepo

    @classmethod
    def from_db(cls, db: Database) -> _ToolDeps:
        """Create the repository set backed by one database path."""
        return cls(
            db=db,
            batches=BatchesRepo(db),
            commands=CommandsRepo(db),
            connections=ConnectionsRepo(db),
            mode=AppModeRepo(db),
            settings=ServerSettingsRepo(db),
        )


def _content(payload: ToolPayload) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]


def _result(payload: ToolPayload, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=_content(payload), is_error=is_error)


def _tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="propose_command",
            description=(
                "Register a command for execution. Normally it queues for human "
                "approval in cgate watch, but if cgate's global mode is AUTO AND "
                "the target server has opted into auto-execution, it runs immediately "
                "and the response includes the result. The response always carries "
                "`mode`, `server_auto_allowed`, and `effective_reason` so you can tell "
                "which path it took. A command matching a known high-blast-radius "
                "pattern (e.g. a recursive force-delete, a raw disk write, deleting "
                "shadow copies/backups) always queues for a human regardless of AUTO "
                "mode -- `effective_reason` comes back as `risky_command` and "
                "`risk_label` names what was matched; this is a heuristic, not a "
                "guarantee, so don't rely on its absence to mean a command is safe. "
                "batch_title is required; batch_description is optional. If batch_id "
                "is omitted, or refers to a batch that no longer exists, a new batch "
                "is created -- check the returned batch_id, it may differ from what "
                'you passed. When the response comes back status: "pending", '
                "nothing has executed yet; call check_status with the returned "
                "batch_id later to learn the outcome."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_alias": {
                        "type": "string",
                        "description": "Alias of a saved connection from list_connections.",
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "Exact command to register, written in the target server's "
                            "dialect: PowerShell for a WINDOWS server_alias (it runs via "
                            "a live PowerShell session, not cmd.exe), POSIX shell for a "
                            "LINUX one. Check server_type via list_connections first if "
                            "unsure."
                        ),
                    },
                    "batch_id": {
                        "type": "string",
                        "description": (
                            "Optional existing batch ID to append to. If it no longer "
                            "exists, a new batch is created instead -- compare against "
                            "the returned batch_id."
                        ),
                    },
                    "batch_title": {
                        "type": "string",
                        "description": "Required one-line purpose of the batch.",
                    },
                    "batch_description": {
                        "type": "string",
                        "description": "Optional one-line explanation of why the batch is needed.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Optional justification for why this command is needed. "
                            "Stored and shown to the human reviewer in cgate watch."
                        ),
                    },
                },
                "required": ["server_alias", "command", "batch_title"],
            },
        ),
        types.Tool(
            name="list_connections",
            description=(
                "List saved server connections and their server_type so an agent can "
                "select a valid alias and command dialect. Each entry also carries "
                "`auto_allowed`, the per-server auto-execution opt-in flag."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_mode",
            description=(
                "Report the current global execution mode and which servers are "
                "opted in for auto-execution. Read-only; safe to call any time to "
                "learn what `propose_command` would do before invoking it."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="check_status",
            description=(
                "Return every command in a batch with status, result, and audit fields. "
                "States are pending, approved, rejected, executed, and failed. Call "
                'this after a propose_command that returned status: "pending" to '
                "learn whether a human approved or rejected it, and to retrieve the "
                "result once it executes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "batch_id": {
                        "type": "string",
                        "description": "ID of the batch to inspect.",
                    }
                },
                "required": ["batch_id"],
            },
        ),
    ]


@asynccontextmanager
async def _lifespan(_server: Server[None]) -> AsyncGenerator[None]:
    yield None


def build_server() -> Server[None]:
    """Build a stateless MCP server configured with command-gate's four tools."""

    async def on_list_tools(
        _context: ServerRequestContext[None, types.PaginatedRequestParams],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tools())

    async def on_call_tool(
        _context: ServerRequestContext[None, types.CallToolRequestParams],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        db = Database(path=data_dir() / "cgate.db")
        init_database(db)
        deps = _ToolDeps.from_db(db)
        arguments = params.arguments or {}
        try:
            match params.name:
                case "propose_command":
                    payload = propose_command(
                        db=deps.db,
                        batches_repo=deps.batches,
                        commands_repo=deps.commands,
                        connections_repo=deps.connections,
                        mode_repo=deps.mode,
                        settings_repo=deps.settings,
                        server_alias=arguments["server_alias"],
                        command=arguments["command"],
                        batch_title=arguments.get("batch_title"),
                        batch_description=arguments.get("batch_description"),
                        batch_id=arguments.get("batch_id"),
                        reason=arguments.get("reason"),
                    )
                case "list_connections":
                    payload = list_connections(
                        connections_repo=deps.connections,
                        settings_repo=deps.settings,
                    )
                case "get_mode":
                    payload = get_mode(mode_repo=deps.mode, settings_repo=deps.settings)
                case "check_status":
                    payload = check_status(
                        batches_repo=deps.batches,
                        commands_repo=deps.commands,
                        batch_id=arguments["batch_id"],
                    )
                case _:
                    return _result(
                        {"error": "unknown_tool", "message": f"unknown tool '{params.name}'"},
                        is_error=True,
                    )
        except ToolError as exc:
            return _result({"error": exc.code, "message": exc.message}, is_error=True)
        except KeyError as exc:
            return _result(
                {
                    "error": "missing_argument",
                    "message": f"missing required argument: {exc.args[0]}",
                },
                is_error=True,
            )
        return _result(payload)

    return Server(
        "command-gate",
        version=__version__,
        instructions=_SERVER_INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        lifespan=_lifespan,
    )


async def _serve_stdio() -> None:
    server = build_server()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Run the command-gate MCP server over standard input and output.

    Spawns the helper to self-heal any staged update before we enter the
    long-running stdio loop; the helper waits on our PID and does the
    swap the instant the IA client that owns us closes the connection.
    """
    maybe_heal_pending_update()
    anyio.run(_serve_stdio)

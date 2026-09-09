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

from cgate.connections.store import ConnectionsRepo
from cgate.core.paths import data_dir
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, init_database
from cgate.mcp_server.tools import (
    BatchStatusResult,
    ConnectionResult,
    ProposeCommandResult,
    ToolError,
    check_status,
    list_connections,
    propose_command,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

ToolPayload: TypeAlias = (
    ProposeCommandResult | BatchStatusResult | list[ConnectionResult] | dict[str, str]
)


@dataclass(frozen=True, slots=True)
class _ToolDeps:
    """Repositories opened independently for one MCP tool invocation."""

    batches: BatchesRepo
    commands: CommandsRepo
    connections: ConnectionsRepo

    @classmethod
    def from_db(cls, db: Database) -> _ToolDeps:
        """Create the repository set backed by one database path."""
        return cls(
            batches=BatchesRepo(db),
            commands=CommandsRepo(db),
            connections=ConnectionsRepo(db),
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
                "Register a command in the local approval queue. NEVER executes. "
                "batch_title is required; batch_description is optional. If batch_id "
                "is omitted, a new batch is created."
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
                        "description": "Exact shell or PowerShell command to register.",
                    },
                    "batch_id": {
                        "type": "string",
                        "description": "Optional existing batch ID to append to.",
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
                        "description": "Optional Phase 1 justification; accepted but not stored.",
                    },
                },
                "required": ["server_alias", "command", "batch_title"],
            },
        ),
        types.Tool(
            name="list_connections",
            description=(
                "List saved server connections and their server_type so an agent can "
                "select a valid alias and command dialect."
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
                "States are pending, approved, rejected, executed, and failed."
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
    """Build a stateless MCP server configured with command-gate's three tools."""

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
                        batches_repo=deps.batches,
                        commands_repo=deps.commands,
                        connections_repo=deps.connections,
                        server_alias=arguments["server_alias"],
                        command=arguments["command"],
                        batch_title=arguments.get("batch_title"),
                        batch_description=arguments.get("batch_description"),
                        batch_id=arguments.get("batch_id"),
                        reason=arguments.get("reason"),
                    )
                case "list_connections":
                    payload = list_connections(connections_repo=deps.connections)
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
        version="0.1.0",
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
    """Run the command-gate MCP server over standard input and output."""
    anyio.run(_serve_stdio)

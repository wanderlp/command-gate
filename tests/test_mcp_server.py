from __future__ import annotations

import json
from typing import TYPE_CHECKING

import anyio
import mcp.types as mcp_types
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

import cgate.executor.selector
import cgate.mcp_server.server as server_module
from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, init_database
from cgate.db.types import BatchId, CommandStatus, ServerType
from cgate.mcp_server import build_server
from cgate.mcp_server.tools import (
    ProposeCommandResult,
    ToolError,
    check_status,
    list_connections,
    propose_command,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def _db(tmp_path: Path) -> Database:
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    return db


def _repos(db: Database) -> tuple[BatchesRepo, CommandsRepo, ConnectionsRepo]:
    return BatchesRepo(db), CommandsRepo(db), ConnectionsRepo(db)


def _add_connection(db: Database, alias: str = "srv") -> None:
    _ = ConnectionsRepo(db).add(
        alias=alias,
        hostname=f"{alias}.local",
        server_type=ServerType.WINDOWS,
        detection_ssh=False,
        detection_winrm=True,
    )


def _propose(db: Database, **overrides: str | None) -> ProposeCommandResult:
    batches, commands, connections = _repos(db)
    values = {
        "server_alias": "srv",
        "command": "Get-Service",
        "batch_title": "diagnostics",
        "batch_description": None,
        **overrides,
    }
    return propose_command(
        batches_repo=batches,
        commands_repo=commands,
        connections_repo=connections,
        server_alias=values["server_alias"] or "",
        command=values["command"] or "",
        batch_title=values["batch_title"],
        batch_description=values["batch_description"],
        batch_id=values.get("batch_id"),
    )


def test_propose_command_creates_new_batch_when_batch_id_omitted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    result = _propose(db)
    assert result["position"] == 0
    assert result["status"] == "pending"
    assert BatchesRepo(db).get(BatchId(str(result["batch_id"]))) is not None


def test_propose_command_appends_to_existing_batch_when_batch_id_supplied(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    first = _propose(db)
    second = _propose(db, batch_id=str(first["batch_id"]))
    assert second["batch_id"] == first["batch_id"]
    assert second["position"] == 1


@pytest.mark.parametrize("title", [None, "   "])
def test_propose_command_raises_when_batch_title_missing(tmp_path: Path, title: str | None) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    with pytest.raises(ToolError, match="batch_title is required"):
        _ = _propose(db, batch_title=title)


def test_propose_command_raises_when_batch_title_empty_string(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    with pytest.raises(ToolError) as exc_info:
        _ = _propose(db, batch_title="")
    assert exc_info.value.code == "missing_batch_title"


def test_propose_command_raises_on_unknown_alias(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as exc_info:
        _ = _propose(_db(tmp_path))
    assert exc_info.value.code == "unknown_alias"


def test_propose_command_records_requested_by_agent_mcp_default(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    result = _propose(db)
    batch = BatchesRepo(db).get(BatchId(str(result["batch_id"])))
    assert batch is not None
    assert batch.requested_by_agent == "mcp"


def test_propose_command_never_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    _add_connection(db)

    def fail_if_called(*_args: str, **_kwargs: str) -> None:
        pytest.fail("executor was called")

    monkeypatch.setattr(cgate.executor.selector, "execute_command", fail_if_called)
    result = _propose(db)
    assert result["status"] == "pending"


def test_list_connections_returns_alias_and_server_type_for_each(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db, "beta")
    _add_connection(db, "alpha")
    result = list_connections(connections_repo=ConnectionsRepo(db))
    assert [(item["alias"], item["server_type"]) for item in result] == [
        ("alpha", "windows"),
        ("beta", "windows"),
    ]


def test_list_connections_returns_empty_list_when_no_saved_connections(tmp_path: Path) -> None:
    assert list_connections(connections_repo=ConnectionsRepo(_db(tmp_path))) == []


def test_check_status_returns_batch_metadata_and_commands_in_order(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    first = _propose(db, command="Get-Service A")
    _ = _propose(db, command="Get-Service B", batch_id=str(first["batch_id"]))
    batches, commands, _connections = _repos(db)
    result = check_status(
        batches_repo=batches,
        commands_repo=commands,
        batch_id=str(first["batch_id"]),
    )
    assert result["title"] == "diagnostics"
    assert [item["command"] for item in result["commands"]] == ["Get-Service A", "Get-Service B"]


def test_check_status_raises_when_batch_unknown(tmp_path: Path) -> None:
    batches, commands, _connections = _repos(_db(tmp_path))
    with pytest.raises(ToolError) as exc_info:
        _ = check_status(batches_repo=batches, commands_repo=commands, batch_id="missing")
    assert exc_info.value.code == "unknown_batch"


def test_check_status_includes_result_and_approved_by_per_command(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    proposed = _propose(db)
    commands = CommandsRepo(db)
    commands.update_status(
        next(iter(commands.list_for_batch(BatchId(str(proposed["batch_id"]))))).id,
        status=CommandStatus.EXECUTED,
        approved_by="operator",
        result="running",
    )
    result = check_status(
        batches_repo=BatchesRepo(db), commands_repo=commands, batch_id=str(proposed["batch_id"])
    )
    assert result["commands"][0]["result"] == "running"
    assert result["commands"][0]["approved_by"] == "operator"


async def _with_client(action: Callable[[ClientSession], Awaitable[None]]) -> None:
    server = build_server()
    async with (
        create_client_server_memory_streams() as (client_streams, server_streams),
        anyio.create_task_group() as task_group,
    ):
            task_group.start_soon(
                server.run, *server_streams, server.create_initialization_options()
            )
            async with ClientSession(*client_streams) as session:
                _ = await session.initialize()
                await action(session)
            task_group.cancel_scope.cancel()


def test_mcp_server_lists_three_tools_on_list_tools() -> None:
    async def action(session: ClientSession) -> None:
        result = await session.list_tools()
        assert {tool.name for tool in result.tools} == {
            "propose_command", "list_connections", "check_status"
        }

    anyio.run(_with_client, action)


def test_mcp_server_routes_propose_command_through_tool_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    monkeypatch.setattr(server_module, "data_dir", lambda: tmp_path)

    async def action(session: ClientSession) -> None:
        result = await session.call_tool(
            "propose_command",
            {"server_alias": "srv", "command": "hostname", "batch_title": "identify"},
        )
        assert isinstance(result, mcp_types.CallToolResult)
        assert isinstance(result.content[0], mcp_types.TextContent)
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "pending"

    anyio.run(_with_client, action)


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (
            {"server_alias": "missing", "command": "hostname", "batch_title": "identify"},
            "unknown_alias",
        ),
        ({"server_alias": "missing", "batch_title": "identify"}, "missing_argument"),
    ],
)
def test_mcp_server_returns_error_text_on_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, str], error: str
) -> None:
    monkeypatch.setattr(server_module, "data_dir", lambda: tmp_path)

    async def action(session: ClientSession) -> None:
        result = await session.call_tool("propose_command", arguments)
        assert isinstance(result, mcp_types.CallToolResult)
        assert isinstance(result.content[0], mcp_types.TextContent)
        assert json.loads(result.content[0].text)["error"] == error

    anyio.run(_with_client, action)


def test_mcp_server_returns_error_on_missing_required_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "data_dir", lambda: tmp_path)

    async def action(session: ClientSession) -> None:
        result = await session.call_tool("check_status", {})
        assert isinstance(result, mcp_types.CallToolResult)
        assert isinstance(result.content[0], mcp_types.TextContent)
        assert json.loads(result.content[0].text)["error"] == "missing_argument"

    anyio.run(_with_client, action)

from __future__ import annotations

import getpass
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
from cgate.db.mode import AppModeRepo, Mode
from cgate.db.server_settings import ServerSettingsRepo
from cgate.db.types import BatchId, CommandId, CommandStatus, ServerType
from cgate.executor.base import ExecutionResult
from cgate.mcp_server import build_server
from cgate.mcp_server.tools import (
    ProposeCommandResult,
    ToolError,
    check_status,
    get_mode,
    list_connections,
    propose_command,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from cgate.db.types import Connection


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
        "reason": None,
        **overrides,
    }
    return propose_command(
        db=db,
        batches_repo=batches,
        commands_repo=commands,
        connections_repo=connections,
        mode_repo=AppModeRepo(db),
        settings_repo=ServerSettingsRepo(db),
        server_alias=values["server_alias"] or "",
        command=values["command"] or "",
        batch_title=values["batch_title"],
        batch_description=values["batch_description"],
        batch_id=values.get("batch_id"),
        reason=values["reason"],
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


def test_propose_command_persists_reason(tmp_path: Path) -> None:
    """Regression: `reason` used to be accepted and immediately discarded --
    the human reviewing in `cgate watch` never saw why the agent wanted to
    run the command at all."""
    db = _db(tmp_path)
    _add_connection(db)
    result = _propose(db, reason="disk is at 95%, need to check what's eating it")
    assert result.get("reason") == "disk is at 95%, need to check what's eating it"
    batch_id = BatchId(str(result["batch_id"]))
    stored = CommandsRepo(db).list_for_batch(batch_id)
    assert stored[0].reason == "disk is at 95%, need to check what's eating it"


def test_propose_command_reason_defaults_to_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    result = _propose(db)
    assert result.get("reason") is None


def test_check_status_includes_reason_for_each_command(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    result = _propose(db, reason="scheduled maintenance window")
    status = check_status(
        batches_repo=BatchesRepo(db),
        commands_repo=CommandsRepo(db),
        batch_id=str(result["batch_id"]),
    )
    assert status["commands"][0]["reason"] == "scheduled maintenance window"


def test_propose_command_queues_in_propose_mode(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    _ = AppModeRepo(db).set(mode=Mode.PROPOSE, updated_by="test")
    result = _propose(db)
    assert result["status"] == "pending"
    assert result.get("mode") == "propose"
    assert result.get("effective_reason") == "global_propose"


def test_propose_command_queues_in_auto_mode_when_server_not_opted_in(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    _ = AppModeRepo(db).set(mode=Mode.AUTO, updated_by="test")
    result = _propose(db)
    assert result["status"] == "pending"
    assert result.get("mode") == "auto"
    assert result.get("server_auto_allowed") is False
    assert result.get("effective_reason") == "server_not_opted_in"


def test_propose_command_executes_in_auto_mode_when_server_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    _ = AppModeRepo(db).set(mode=Mode.AUTO, updated_by="test")
    _ = ServerSettingsRepo(db).set(alias="srv", auto_allowed=True, updated_by="test")

    def fake_execute(_connection: Connection, _command: str, **_kwargs: object) -> ExecutionResult:
        return ExecutionResult(
            stdout="ok-output", stderr="", exit_code=0, duration_ms=1, error_kind=None
        )

    monkeypatch.setattr(cgate.executor.selector, "execute_command", fake_execute)
    monkeypatch.setattr(getpass, "getuser", lambda: "testuser")
    result = _propose(db)
    assert result["status"] == "executed"
    assert result.get("effective_reason") == "both_allowed"
    assert result.get("result") == "ok-output"
    approved_by = result.get("approved_by")
    assert approved_by is not None
    assert approved_by.startswith("auto:watch:")
    # Regression: auto-execution bypasses watch/approval.py entirely, so
    # nothing else stamps `resolved_at` -- an unresolved batch sits at the
    # head of the FIFO queue forever, blocking every batch behind it even
    # though it has nothing left to approve or reject.
    batch = BatchesRepo(db).get(BatchId(str(result["batch_id"])))
    assert batch is not None
    assert batch.resolved_at is not None


def test_propose_command_auto_delegates_to_execute_and_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #36 review blocker #1: the MCP AUTO path used to duplicate
    ``execute_and_finalize``'s body inline (PENDING -> APPROVED ->
    execute_command -> terminal write), bypassing the CAS-claim guard
    that prevents two callers from racing through to a double remote
    execution. The refactor delegates to the shared
    ``execute_and_finalize`` from ``cgate.executor.lifecycle`` -- the same
    function the watch TUI's ``approve_one`` uses, so the two callers
    cannot diverge.

    This test verifies the delegation by checking that ``execute_command``
    is called via ``cgate.executor.selector`` -- the module the shared
    lifecycle imports -- rather than through a private path. A monkeypatch
    on ``cgate.executor.selector.execute_command`` is what reaches the
    test's fake. If the MCP path had its own inline call, the
    monkeypatch would NOT be observed (the test would see the real
    executor). The test therefore pins the architectural decision, not just
    the end behavior.

    We also assert ``claimed_at`` was stamped, which is the lifecycle's
    signature on the way through the EXECUTING CAS.
    """
    db = _db(tmp_path)
    _add_connection(db)
    _ = AppModeRepo(db).set(mode=Mode.AUTO, updated_by="test")
    _ = ServerSettingsRepo(db).set(alias="srv", auto_allowed=True, updated_by="test")

    captured: list[str] = []

    def fake_execute(_connection: Connection, _command: str, **_kwargs: object) -> ExecutionResult:
        # Read the row's claimed_at via the executor's commands_repo is
        # not in scope here, but the call itself proves the shared lifecycle
        # was used (otherwise ``cgate.executor.selector.execute_command``
        # would not have been called).
        captured.append("called")
        return ExecutionResult(
            stdout="ok", stderr="", exit_code=0, duration_ms=1, error_kind=None
        )

    monkeypatch.setattr(cgate.executor.selector, "execute_command", fake_execute)
    monkeypatch.setattr(getpass, "getuser", lambda: "testuser")
    result = _propose(db)
    assert result["status"] == "executed"
    assert captured == ["called"]
    # The CAS-claim stamp is the only durable proof that we went through
    # ``execute_and_finalize`` (vs. an inline duplicate). Re-fetch the
    # command and check claimed_at is non-NULL.
    cmd_id = result["command_id"]
    cmd_row = CommandsRepo(db).get(CommandId(cmd_id))
    assert cmd_row is not None
    assert cmd_row.claimed_at is not None, (
        "claimed_at is the lifecycle's stamp on the CAS-claim step; if "
        "it is NULL the MCP path went through an inline duplicate of "
        "the lifecycle, bypassing the CAS-claim guard."
    )


def test_propose_command_queues_a_risky_command_even_in_auto_mode_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high-blast-radius command must never auto-run, even with both
    switches (global AUTO + per-server opt-in) already flipped."""
    db = _db(tmp_path)
    _add_connection(db)
    _ = AppModeRepo(db).set(mode=Mode.AUTO, updated_by="test")
    _ = ServerSettingsRepo(db).set(alias="srv", auto_allowed=True, updated_by="test")

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        pytest.fail("executor was called for a risky command")

    monkeypatch.setattr(cgate.executor.selector, "execute_command", fail_if_called)
    result = _propose(db, command="Remove-Item -Recurse -Force C:\\")
    assert result["status"] == "pending"
    assert result.get("effective_reason") == "risky_command"
    assert result.get("risk_label") is not None


def test_propose_command_marks_risk_label_none_for_a_safe_command(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    result = _propose(db)
    assert result.get("risk_label") is None


def test_check_status_includes_risk_label_for_each_command(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    result = _propose(db, command="Remove-Item -Recurse -Force C:\\")
    status = check_status(
        batches_repo=BatchesRepo(db),
        commands_repo=CommandsRepo(db),
        batch_id=str(result["batch_id"]),
    )
    assert status["commands"][0]["risk_label"] is not None


def test_list_connections_returns_alias_and_server_type_for_each(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db, "beta")
    _add_connection(db, "alpha")
    result = list_connections(
        connections_repo=ConnectionsRepo(db), settings_repo=ServerSettingsRepo(db)
    )
    assert [(item["alias"], item["server_type"]) for item in result] == [
        ("alpha", "windows"),
        ("beta", "windows"),
    ]


def test_list_connections_returns_empty_list_when_no_saved_connections(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = list_connections(
        connections_repo=ConnectionsRepo(db), settings_repo=ServerSettingsRepo(db)
    )
    assert result == []


def test_list_connections_includes_auto_allowed_false_by_default(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    result = list_connections(
        connections_repo=ConnectionsRepo(db), settings_repo=ServerSettingsRepo(db)
    )
    assert result[0]["auto_allowed"] is False


def test_list_connections_includes_auto_allowed_true_when_opted_in(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db)
    _ = ServerSettingsRepo(db).set(alias="srv", auto_allowed=True, updated_by="test")
    result = list_connections(
        connections_repo=ConnectionsRepo(db), settings_repo=ServerSettingsRepo(db)
    )
    assert result[0]["auto_allowed"] is True


def test_list_connections_mixes_opted_in_and_default(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _add_connection(db, "alpha")
    _add_connection(db, "beta")
    _ = ServerSettingsRepo(db).set(alias="beta", auto_allowed=True, updated_by="test")
    result = list_connections(
        connections_repo=ConnectionsRepo(db), settings_repo=ServerSettingsRepo(db)
    )
    assert {item["alias"]: item["auto_allowed"] for item in result} == {
        "alpha": False,
        "beta": True,
    }


def test_get_mode_returns_propose_when_mode_unset(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = get_mode(mode_repo=AppModeRepo(db), settings_repo=ServerSettingsRepo(db))
    assert result == {"mode": "propose", "auto_allowed_servers": []}


def test_get_mode_returns_auto_when_mode_set(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _ = AppModeRepo(db).set(mode=Mode.AUTO, updated_by="test")
    result = get_mode(mode_repo=AppModeRepo(db), settings_repo=ServerSettingsRepo(db))
    assert result["mode"] == "auto"


def test_get_mode_lists_only_auto_allowed_servers(tmp_path: Path) -> None:
    db = _db(tmp_path)
    settings = ServerSettingsRepo(db)
    _ = settings.set(alias="alpha", auto_allowed=True, updated_by="test")
    _ = settings.set(alias="beta", auto_allowed=False, updated_by="test")
    _ = settings.set(alias="gamma", auto_allowed=True, updated_by="test")
    result = get_mode(mode_repo=AppModeRepo(db), settings_repo=settings)
    assert result["auto_allowed_servers"] == ["alpha", "gamma"]


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


def test_mcp_server_lists_four_tools_on_list_tools() -> None:
    async def action(session: ClientSession) -> None:
        result = await session.list_tools()
        assert {tool.name for tool in result.tools} == {
            "propose_command", "list_connections", "check_status", "get_mode"
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

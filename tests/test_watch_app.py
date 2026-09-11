"""Headless Textual pilot tests for the `cgate watch` dashboard."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, init_database
from cgate.db.types import CommandStatus, ServerType
from cgate.executor.base import ExecutionResult
from cgate.watch.app import ActivePanel, WatchApp

if TYPE_CHECKING:
    from pathlib import Path

COMMAND_COUNT = 3


@dataclass(frozen=True, slots=True)
class Repos:
    db: Database
    batches: BatchesRepo
    commands: CommandsRepo
    connections: ConnectionsRepo


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    return Repos(
        db=db,
        batches=BatchesRepo(db),
        commands=CommandsRepo(db),
        connections=ConnectionsRepo(db),
    )


def _app(repos: Repos) -> WatchApp:
    return WatchApp(
        db=repos.db,
        batches=repos.batches,
        commands=repos.commands,
        connections=repos.connections,
    )


def _success() -> ExecutionResult:
    return ExecutionResult("hello\n", "", 0, 42, None)


def test_idle_state_when_queue_is_empty(repos: Repos) -> None:
    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            panel = pilot.app.query_one(ActivePanel)
            return str(panel.query_one("#active-header").content)

    header_text = asyncio.run(scenario())
    assert "Sin lotes pendientes" in header_text


def test_approve_one_executes_and_advances(repos: Repos) -> None:
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    first = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )

    async def scenario() -> None:
        with patch("cgate.watch.approval.execute_command", return_value=_success()):
            async with _app(repos).run_test() as pilot:
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()

    asyncio.run(scenario())

    updated = repos.commands.get(first.id)
    assert updated is not None
    assert updated.status is CommandStatus.EXECUTED


def test_reject_all_resolves_the_active_batch(repos: Repos) -> None:
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    for index in range(COMMAND_COUNT):
        _ = repos.commands.add(
            batch_id=lot.id,
            server_alias="linux-1",
            server_type=ServerType.LINUX,
            command=str(index),
        )

    async def scenario() -> None:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()

    asyncio.run(scenario())

    commands_in_lot = repos.commands.list_for_batch(lot.id)
    assert all(command.status is CommandStatus.REJECTED for command in commands_in_lot)
    resolved = repos.batches.get(lot.id)
    assert resolved is not None
    assert resolved.resolved_at is not None

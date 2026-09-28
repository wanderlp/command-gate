"""Headless Textual pilot tests for the mode and server-settings modals."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Static

from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, init_database
from cgate.db.mode import AppModeNotSetError, AppModeRepo, Mode
from cgate.db.server_settings import ServerSettingsRepo
from cgate.db.types import ServerType
from cgate.watch.app import WatchApp
from cgate.watch.mode_modal import ModeModal
from cgate.watch.server_settings_modal import ServerRow, ServerSettingsModal

if TYPE_CHECKING:
    from pathlib import Path

CONNECTION_COUNT = 2


@dataclass(frozen=True, slots=True)
class Repos:
    db: Database
    batches: BatchesRepo
    commands: CommandsRepo
    connections: ConnectionsRepo
    mode: AppModeRepo
    server_settings: ServerSettingsRepo


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    db = Database(path=tmp_path / "cgate.db")
    init_database(db)
    return Repos(
        db=db,
        batches=BatchesRepo(db),
        commands=CommandsRepo(db),
        connections=ConnectionsRepo(db),
        mode=AppModeRepo(db),
        server_settings=ServerSettingsRepo(db),
    )


def _app(repos: Repos) -> WatchApp:
    return WatchApp(
        db=repos.db,
        batches=repos.batches,
        commands=repos.commands,
        connections=repos.connections,
        mode=repos.mode,
        server_settings=repos.server_settings,
    )


def _add_connection(repos: Repos, alias: str, server_type: ServerType) -> None:
    _ = repos.connections.add(
        alias=alias,
        hostname=f"{alias}.example",
        server_type=server_type,
        detection_ssh=True,
        detection_winrm=False,
    )


def _mode_indicator(app: WatchApp) -> str:
    return str(app.query_one("#mode-indicator").content)


def test_header_shows_propose_by_default(repos: Repos) -> None:
    async def scenario() -> str:
        app = _app(repos)
        async with app.run_test() as pilot:
            await pilot.pause()
            return _mode_indicator(app)

    header = asyncio.run(scenario())
    assert "MODE:" in header
    assert "PROPOSE" in header
    assert "auto-allowed" not in header


def test_header_shows_auto_with_server_count(repos: Repos) -> None:
    _ = repos.mode.set(mode=Mode.AUTO, updated_by="test")
    _add_connection(repos, "linux-1", ServerType.LINUX)
    _add_connection(repos, "win-1", ServerType.WINDOWS)
    _ = repos.server_settings.set(alias="linux-1", auto_allowed=True, updated_by="test")

    async def scenario() -> str:
        app = _app(repos)
        async with app.run_test() as pilot:
            await pilot.pause()
            return _mode_indicator(app)

    header = asyncio.run(scenario())
    assert "AUTO" in header
    assert "1 servers auto-allowed (of 2)" in header


def test_mode_modal_confirm_switches_mode(repos: Repos) -> None:
    async def scenario() -> str:
        app = _app(repos)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ModeModal)
            await pilot.press("y")
            await pilot.pause()
            return _mode_indicator(app)

    header = asyncio.run(scenario())
    assert repos.mode.get().mode is Mode.AUTO
    assert "AUTO" in header


def test_mode_modal_cancel_writes_nothing(repos: Repos) -> None:
    async def scenario() -> None:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ModeModal)
            await pilot.press("x")
            await pilot.pause()
            assert not isinstance(pilot.app.screen, ModeModal)

    asyncio.run(scenario())
    with pytest.raises(AppModeNotSetError):
        repos.mode.get()


def test_mode_modal_from_auto_targets_propose(repos: Repos) -> None:
    _ = repos.mode.set(mode=Mode.AUTO, updated_by="test")

    async def scenario() -> None:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

    asyncio.run(scenario())
    assert repos.mode.get().mode is Mode.PROPOSE


def test_server_settings_modal_lists_all_connections(repos: Repos) -> None:
    _add_connection(repos, "linux-1", ServerType.LINUX)
    _add_connection(repos, "win-1", ServerType.WINDOWS)

    async def scenario() -> int:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, ServerSettingsModal)
            return len(screen.query(ServerRow))

    row_count = asyncio.run(scenario())
    assert row_count == CONNECTION_COUNT


def test_server_settings_space_toggles_and_enter_persists(repos: Repos) -> None:
    _add_connection(repos, "linux-1", ServerType.LINUX)
    _add_connection(repos, "win-1", ServerType.WINDOWS)

    async def scenario() -> None:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(pilot.app.screen, ServerSettingsModal)

    asyncio.run(scenario())
    toggled = repos.server_settings.get("linux-1")
    assert toggled is not None
    assert toggled.auto_allowed is True
    # The untouched row must not be persisted at all.
    assert repos.server_settings.get("win-1") is None


def test_server_settings_escape_discards_changes(repos: Repos) -> None:
    _add_connection(repos, "linux-1", ServerType.LINUX)

    async def scenario() -> None:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(pilot.app.screen, ServerSettingsModal)

    asyncio.run(scenario())
    assert repos.server_settings.get("linux-1") is None


def test_sidebar_lists_connections_with_flags(repos: Repos) -> None:
    _add_connection(repos, "linux-1", ServerType.LINUX)
    _add_connection(repos, "win-1", ServerType.WINDOWS)
    _ = repos.server_settings.set(alias="win-1", auto_allowed=True, updated_by="test")

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            items = pilot.app.query_one("#servers-list").children
            return "\n".join(str(item.query_one("Static").content) for item in items)

    content = asyncio.run(scenario())
    assert "linux-1" in content
    assert "win-1" in content
    assert "[ ]" in content
    assert "[✓]" in content


def test_server_settings_modal_renders_a_connection_alias_with_brackets(repos: Repos) -> None:
    """Regression for issue #32: an alias like ``srv[prod]`` would crash
    ``ServerSettingsModal`` with ``MarkupError`` because ``_row_markup``
    interpolated the alias without escaping. Now the brackets render
    literally."""
    _add_connection(repos, "srv[prod]", ServerType.WINDOWS)

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            return "\n".join(str(row.query_one(Static).content) for row in pilot.app.screen.query_one("#settings-list").children)

    rendered = asyncio.run(scenario())
    assert "srv\\[prod\\]" in rendered

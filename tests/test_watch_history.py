"""Headless Textual pilot tests for the History browser and command-detail modal."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from textual.widgets import Input, ListView, Static

from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, init_database
from cgate.db.mode import AppModeRepo
from cgate.db.server_settings import ServerSettingsRepo
from cgate.db.types import CommandStatus, ServerType
from cgate.watch.app import WatchApp
from cgate.watch.command_detail_modal import CommandDetailModal
from cgate.watch.history_modal import HistoryModal

if TYPE_CHECKING:
    from pathlib import Path

    from textual.pilot import Pilot


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


def _resolved_batch_with_command(
    repos: Repos,
    *,
    title: str,
    reason: str | None = None,
    command: str = "uptime",
    server_alias: str = "linux-1",
) -> None:
    lot = repos.batches.create(title=title, description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id,
        server_alias=server_alias,
        server_type=ServerType.LINUX,
        command=command,
        reason=reason,
    )
    _ = repos.commands.update_status(
        cmd.id, status=CommandStatus.EXECUTED, approved_by="wlopez", result="14:32 up 3 days"
    )
    repos.batches.mark_resolved(lot.id)


def _batch_titles(pilot: Pilot[None]) -> list[str]:
    """Titles currently shown in #history-list, in order."""
    list_view = pilot.app.screen.query_one("#history-list", ListView)
    return [str(child.query_one(Static).content) for child in list_view.children]


def test_h_key_opens_the_history_modal(repos: Repos) -> None:
    async def scenario() -> bool:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            return isinstance(pilot.app.screen, HistoryModal)

    assert asyncio.run(scenario())


def test_history_shows_nothing_yet_when_no_batch_has_resolved(repos: Repos) -> None:
    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            return str(pilot.app.screen.query_one("#history-detail-header", Static).content)

    header = asyncio.run(scenario())
    assert "No resolved batches" in header


def test_history_lists_resolved_batch_and_its_commands(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="past deploy")

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            return str(pilot.app.screen.query_one("#history-detail-header", Static).content)

    header = asyncio.run(scenario())
    assert "past deploy" in header


def test_history_never_shows_a_batch_still_pending(repos: Repos) -> None:
    """The whole point of this screen is resolved batches -- the live queue
    already shows anything still pending."""
    _resolved_batch_with_command(repos, title="done already")
    still_open = repos.batches.create(title="still open", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=still_open.id,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command="uptime",
    )

    async def scenario() -> list[str]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            list_view = pilot.app.screen.query_one("#history-list", ListView)
            return [str(child.query_one(Static).content) for child in list_view.children]

    titles = asyncio.run(scenario())
    assert any("done already" in title for title in titles)
    assert not any("still open" in title for title in titles)


def test_entering_a_command_in_history_opens_its_detail(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="past deploy", reason="scheduled patch window")

    async def scenario() -> bool:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            history = pilot.app.screen
            assert isinstance(history, HistoryModal)
            _ = history.query_one("#history-rows", ListView).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(pilot.app.screen, CommandDetailModal)

    assert asyncio.run(scenario())


def test_command_detail_modal_shows_full_result_and_reason(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="past deploy", reason="scheduled patch window")

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            history = pilot.app.screen
            assert isinstance(history, HistoryModal)
            _ = history.query_one("#history-rows", ListView).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            detail = pilot.app.screen
            assert isinstance(detail, CommandDetailModal)
            return str(detail.query_one("#detail-text", Static).content)

    body = asyncio.run(scenario())
    assert "scheduled patch window" in body
    assert "14:32 up 3 days" in body


def test_escape_closes_the_history_modal(repos: Repos) -> None:
    async def scenario() -> bool:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(pilot.app.screen, HistoryModal)
            await pilot.press("escape")
            await pilot.pause()
            return isinstance(pilot.app.screen, HistoryModal)

    still_open = asyncio.run(scenario())
    assert not still_open


def test_slash_focuses_the_filter_input(repos: Repos) -> None:
    async def scenario() -> bool:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            history = pilot.app.screen
            assert isinstance(history, HistoryModal)
            return history.focused is history.query_one("#history-filter", Input)

    assert asyncio.run(scenario())


def test_filter_narrows_by_batch_title(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="deploy frontend")
    _resolved_batch_with_command(repos, title="restart backend")

    async def scenario() -> list[str]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            for char in "frontend":
                await pilot.press(char)
            await pilot.pause()
            return _batch_titles(pilot)

    titles = asyncio.run(scenario())
    assert any("deploy frontend" in title for title in titles)
    assert not any("restart backend" in title for title in titles)


def test_filter_matches_command_text_not_just_the_title(repos: Repos) -> None:
    """The whole point of matching broadly: you remember what you ran
    more often than what you titled the batch."""
    _resolved_batch_with_command(repos, title="maintenance", command="systemctl restart nginx")
    _resolved_batch_with_command(repos, title="other maintenance", command="uptime")

    async def scenario() -> list[str]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            for char in "nginx":
                await pilot.press(char)
            await pilot.pause()
            return _batch_titles(pilot)

    titles = asyncio.run(scenario())
    assert len(titles) == 1
    assert "maintenance" in titles[0]
    assert "other maintenance" not in titles[0]


def test_filter_matches_server_alias(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="lot a", server_alias="db-prod")
    _resolved_batch_with_command(repos, title="lot b", server_alias="web-1")

    async def scenario() -> list[str]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            for char in "db-prod":
                await pilot.press(char)
            await pilot.pause()
            return _batch_titles(pilot)

    titles = asyncio.run(scenario())
    assert any("lot a" in title for title in titles)
    assert not any("lot b" in title for title in titles)


def test_filter_is_case_insensitive(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="Deploy Frontend")

    async def scenario() -> list[str]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            for char in "DEPLOY":
                await pilot.press(char)
            await pilot.pause()
            return _batch_titles(pilot)

    titles = asyncio.run(scenario())
    assert any("Deploy Frontend" in title for title in titles)


def test_filter_shows_no_matches_message_when_nothing_matches(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="deploy frontend")

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            for char in "zzz":
                await pilot.press(char)
            await pilot.pause()
            return str(pilot.app.screen.query_one("#history-detail-header", Static).content)

    header = asyncio.run(scenario())
    assert "No matches" in header


def test_typing_q_in_the_filter_does_not_close_the_modal(repos: Repos) -> None:
    """Regression: `q` closes the modal when the list has focus, but a
    focused filter Input must still receive it as a literal character."""
    _resolved_batch_with_command(repos, title="quick fix")

    async def scenario() -> tuple[bool, str]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("q")
            await pilot.pause()
            history = pilot.app.screen
            still_open = isinstance(history, HistoryModal)
            value = str(history.query_one("#history-filter", Input).value) if still_open else ""
            return still_open, value

    still_open, value = asyncio.run(scenario())
    assert still_open
    assert value == "q"


def test_escape_exits_filter_mode_before_closing_the_modal(repos: Repos) -> None:
    seeded_batches = 2
    _resolved_batch_with_command(repos, title="deploy frontend")
    _resolved_batch_with_command(repos, title="restart backend")

    async def scenario() -> tuple[bool, str, int]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            for char in "frontend":
                await pilot.press(char)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            history = pilot.app.screen
            still_open_after_first_escape = isinstance(history, HistoryModal)
            filter_value = (
                str(history.query_one("#history-filter", Input).value)
                if still_open_after_first_escape
                else ""
            )
            visible_count = len(_batch_titles(pilot)) if still_open_after_first_escape else 0
            return still_open_after_first_escape, filter_value, visible_count

    still_open, filter_value, visible_count = asyncio.run(scenario())
    assert still_open, "the first Escape must exit filter mode, not close the modal"
    assert filter_value == ""
    assert visible_count == seeded_batches  # back to the unfiltered list


def test_enter_in_the_filter_moves_focus_to_the_results_list(repos: Repos) -> None:
    _resolved_batch_with_command(repos, title="deploy frontend")

    async def scenario() -> bool:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.press("slash")
            for char in "deploy":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause()
            history = pilot.app.screen
            assert isinstance(history, HistoryModal)
            return history.focused is history.query_one("#history-list", ListView)

    assert asyncio.run(scenario())


def test_batch_with_brackets_in_title_does_not_crash_history(repos: Repos) -> None:
    """Regression for issue #32: a batch title containing ``[`` or ``]``
    would crash ``BatchHistoryRow`` with ``MarkupError`` because the row
    interpolated the title into markup without escaping. Now the brackets
    render literally."""
    _resolved_batch_with_command(repos, title="Investigate [WinEvent] timeouts")

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            return _batch_titles(pilot)[0]

    rendered = asyncio.run(scenario())
    assert "Investigate \\[WinEvent\\] timeouts" in rendered


def test_history_modal_db_error_with_brackets_does_not_crash(repos: Repos) -> None:
    """Regression for issue #39: ``HistoryModal.on_mount`` used to
    interpolate ``{exc}`` straight into ``Static.update``, so a
    ``sqlite3.Error`` whose message contained ``[`` or ``]`` crashed
    ``rich.errors.MarkupError`` the moment the user opened the History
    screen. The error path must now escape the exception text first.
    """
    bracketed_msg = "no such column: [secret]"

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with patch.object(
                repos.batches,
                "list_resolved",
                side_effect=sqlite3.OperationalError(bracketed_msg),
            ):
                await pilot.press("h")
                await pilot.pause()
            return str(pilot.app.screen.query_one("#history-detail-header", Static).content)

    header = asyncio.run(scenario())
    assert "\\[secret\\]" in header
    assert "[secret]" not in header

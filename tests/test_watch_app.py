"""Headless Textual pilot tests for the `cgate watch` dashboard."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from textual.widgets import ListView

from cgate import __version__
from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.db.connection import Database, init_database
from cgate.db.mode import AppModeRepo
from cgate.db.server_settings import ServerSettingsRepo
from cgate.db.types import CommandStatus, ServerType
from cgate.executor.base import ExecutionResult
from cgate.watch.app import ActivePanel, QueueSidebar, ServersSidebar, WatchApp
from cgate.watch.approval import execute_and_finalize
from cgate.watch.command_detail_modal import CommandDetailModal
from cgate.watch.queue import fail_orphaned_approvals
from cgate.watch.widgets import CommandRow

if TYPE_CHECKING:
    from pathlib import Path

COMMAND_COUNT = 3


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


def _success() -> ExecutionResult:
    return ExecutionResult("hello\n", "", 0, 42, None)


def test_app_registers_and_activates_the_cgate_theme(repos: Repos) -> None:
    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            return str(pilot.app.theme)

    active_theme = asyncio.run(scenario())
    assert active_theme == "cgate"


def test_title_shows_the_installed_cgate_version(repos: Repos) -> None:
    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            return str(pilot.app.query_one("#mode-title").content)

    title_text = asyncio.run(scenario())
    assert "cgate watch" in title_text
    assert __version__ in title_text


def test_idle_state_when_queue_is_empty(repos: Repos) -> None:
    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            panel = pilot.app.query_one(ActivePanel)
            return str(panel.query_one("#active-header").content)

    header_text = asyncio.run(scenario())
    assert "No pending batches" in header_text


def test_risky_command_shows_a_warning_in_the_active_panel_in_propose_mode(repos: Repos) -> None:
    """Item 3: the human should see the flag even in PROPOSE mode (the
    default here), not only when it changes what AUTO mode would do."""
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command="rm -rf /",
        risk_label="recursive force delete (rm -rf)",
    )

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            row = pilot.app.query_one(ActivePanel).query_one(CommandRow)
            return str(row.content)

    row_text = asyncio.run(scenario())
    assert "RISKY" in row_text


def test_queue_sidebar_skips_rebuild_when_nothing_changed(repos: Repos) -> None:
    """The queue sidebar used to clear and rebuild its list view on every
    poll tick regardless of whether anything changed -- visible as every
    row flashing every `_POLL_INTERVAL_SECONDS` for no reason."""
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )

    async def scenario() -> int:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            sidebar = pilot.app.query_one(QueueSidebar)
            list_view = sidebar.query_one("#queue-list", ListView)
            with patch.object(list_view, "clear", wraps=list_view.clear) as clear_spy:
                pilot.app._refresh()  # noqa: SLF001 -- same pending data as on_mount already rendered
                await pilot.pause()
                return clear_spy.call_count

    call_count = asyncio.run(scenario())
    assert call_count == 0


def test_servers_sidebar_skips_rebuild_when_nothing_changed(repos: Repos) -> None:
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )

    async def scenario() -> int:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            sidebar = pilot.app.query_one(ServersSidebar)
            list_view = sidebar.query_one("#servers-list", ListView)
            with patch.object(list_view, "clear", wraps=list_view.clear) as clear_spy:
                pilot.app._refresh()  # noqa: SLF001 -- same connections as on_mount already rendered
                await pilot.pause()
                return clear_spy.call_count

    call_count = asyncio.run(scenario())
    assert call_count == 0


def test_servers_sidebar_renders_a_connection_alias_with_brackets(repos: Repos) -> None:
    """Regression for issue #32: an alias like ``srv[prod]`` would crash
    ``ServersSidebar`` with ``MarkupError`` because ``Static(markup=True)``
    was interpolating the alias without escaping. Now the brackets render
    literally instead of crashing.
    """
    _ = repos.connections.add(
        alias="srv[prod]",
        hostname="srv-prod.example",
        server_type=ServerType.WINDOWS,
        detection_ssh=False,
        detection_winrm=True,
    )

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            sidebar = pilot.app.query_one(ServersSidebar)
            list_view = sidebar.query_one("#servers-list", ListView)
            return str(list_view.children[0].children[0].content)

    rendered = asyncio.run(scenario())
    assert "srv\\[prod\\]" in rendered
    assert "srv[prod]" not in rendered.split("  ", 1)[0]


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
        with patch("cgate.executor.selector.execute_command", return_value=_success()):
            async with _app(repos).run_test() as pilot:
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()

    asyncio.run(scenario())

    updated = repos.commands.get(first.id)
    assert updated is not None
    assert updated.status is CommandStatus.EXECUTED


def test_approve_one_refreshes_between_mark_approved_and_execution(repos: Repos) -> None:
    """Regression: approve_one used to mark-approved + execute in one
    worker-thread call with no chance for the TUI to refresh in between,
    so pressing `y` on a slow command looked exactly like a frozen
    dashboard for however long the executor's timeout allowed. `_approve`
    must now refresh right after marking approved, before the (possibly
    slow) execution call."""
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    calls: list[str] = []

    def fake_mark_approved(**_kwargs: object) -> None:
        calls.append("mark_approved")

    def fake_execute_and_finalize(**_kwargs: object) -> tuple[None, None]:
        calls.append("execute_and_finalize")
        return None, None

    async def scenario() -> None:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with (
                patch("cgate.watch.app.mark_approved", side_effect=fake_mark_approved),
                patch(
                    "cgate.watch.app.execute_and_finalize", side_effect=fake_execute_and_finalize
                ),
                patch.object(
                    pilot.app, "_refresh", side_effect=lambda: calls.append("refresh")
                ),
            ):
                await pilot.press("y")
                await pilot.pause()

    asyncio.run(scenario())

    assert calls == ["mark_approved", "refresh", "execute_and_finalize", "refresh"]


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


def test_selecting_a_batch_in_the_sidebar_pins_it_active(repos: Repos) -> None:
    """A human can jump the queue and prioritize approving a batch further
    down instead of being forced through strict FIFO order."""
    older = repos.batches.create(title="older", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=older.id, server_alias="linux-1", server_type=ServerType.LINUX, command="a"
    )
    newer = repos.batches.create(title="newer", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=newer.id, server_alias="linux-1", server_type=ServerType.LINUX, command="b"
    )

    async def scenario() -> tuple[str, object]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            pilot.app.query_one("#queue-list", ListView).focus()
            await pilot.pause()
            await pilot.press("down")  # highlight the second (newer) batch
            await pilot.press("enter")  # pin it as active
            await pilot.pause()
            header = str(pilot.app.query_one("#active-header").content)
            return header, pilot.app._pinned_batch_id  # noqa: SLF001

    header, pinned_id = asyncio.run(scenario())
    assert "newer" in header
    assert pinned_id == newer.id


def test_approve_after_pinning_acts_on_the_pinned_batch_not_fifo_first(repos: Repos) -> None:
    older = repos.batches.create(title="older", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=older.id, server_alias="linux-1", server_type=ServerType.LINUX, command="a"
    )
    newer = repos.batches.create(title="newer", description=None, requested_by_agent=None)
    newer_cmd = repos.commands.add(
        batch_id=newer.id, server_alias="linux-1", server_type=ServerType.LINUX, command="b"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )

    async def scenario() -> None:
        with patch("cgate.executor.selector.execute_command", return_value=_success()):
            async with _app(repos).run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#queue-list", ListView).focus()
                await pilot.pause()
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()

    asyncio.run(scenario())

    updated_newer = repos.commands.get(newer_cmd.id)
    assert updated_newer is not None
    assert updated_newer.status is CommandStatus.EXECUTED
    older_cmd = repos.commands.list_for_batch(older.id)[0]
    assert older_cmd.status is CommandStatus.PENDING


def test_pinned_batch_falls_back_to_fifo_once_it_resolves(repos: Repos) -> None:
    """Self-correcting: no explicit unpin needed once the chosen batch is done."""
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="a"
    )

    async def scenario() -> None:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            pilot.app.query_one("#queue-list", ListView).focus()
            await pilot.pause()
            await pilot.press("enter")  # pin the only batch (itself)
            await pilot.press("n")  # reject its only command -> the batch resolves
            await pilot.pause()

    asyncio.run(scenario())

    resolved = repos.batches.get(lot.id)
    assert resolved is not None
    assert resolved.resolved_at is not None


def test_entering_a_command_row_opens_the_detail_modal(repos: Repos) -> None:
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command="uptime",
        reason="checking uptime after patching",
    )

    async def scenario() -> bool:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            pilot.app.query_one("#rows", ListView).focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(pilot.app.screen, CommandDetailModal)

    opened = asyncio.run(scenario())
    assert opened


def test_refresh_renders_clean_message_on_sqlite_error(repos: Repos) -> None:
    """issue #12 (reopened): Textual's widget exception handler prints a
    raw ``rich.traceback.Traceback(show_locals=True)`` on any exception
    raised from a timer/handler, bypassing ``cli/main.py``'s
    ``sqlite3.Error`` boundary entirely. ``_refresh`` must catch the
    DB error itself and render a clean in-UI message instead -- the
    next polling tick retries automatically."""

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with patch.object(
                repos.batches,
                "list_pending",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                pilot.app._refresh()  # noqa: SLF001 -- private but the only entry point
                await pilot.pause()
            return str(pilot.app.query_one("#waiting-notice").content)

    out = asyncio.run(scenario())
    assert "database is locked" in out
    assert "locking" in out.lower()


def test_refresh_survives_db_error_in_subsequent_call(repos: Repos) -> None:
    """issue #12 (extended): the original ``_refresh`` catch only
    wrapped ``list_pending``; if ``count_waiting`` or ``list_for_batch``
    raised instead, the watch app would still die on Textual's traceback.
    The full body must be inside one try."""

    async def scenario() -> tuple[str, str]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with patch(
                "cgate.watch.app.count_waiting",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                pilot.app._refresh()  # noqa: SLF001
                await pilot.pause()
            notice = str(pilot.app.query_one("#waiting-notice").content)
            sub = str(pilot.app.sub_title)
            return notice, sub

    notice, sub = asyncio.run(scenario())
    assert "database is locked" in notice
    assert sub == "database error"


def test_first_pending_returns_none_on_db_error(repos: Repos) -> None:
    """issue #12 (extended): ``_first_pending`` powers every action
    handler (approve/reject). If it raised sqlite3.Error, a user keypress
    would trigger Textual's raw traceback. Must catch internally and
    return ``None`` so the action handler is a silent no-op while the
    error stays on screen."""

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with patch.object(
                repos.batches,
                "list_pending",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                result = pilot.app._first_pending()  # noqa: SLF001
                await pilot.pause()
            assert result is None
            return str(pilot.app.query_one("#waiting-notice").content)

    notice = asyncio.run(scenario())
    assert "database is locked" in notice


def test_reject_action_keeps_tui_alive_on_db_error(repos: Repos) -> None:
    """issue #12 (extended): ``action_reject_one`` calls ``reject_one``
    which opens DB. If the DB throws mid-reject, Textual would print a
    raw traceback. The action must keep the TUI alive and surface the
    error inline."""

    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command="uptime",
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )

    async def scenario() -> tuple[str, bool]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with patch.object(
                repos.commands,
                "update_status",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                await pilot.press("n")
                await pilot.pause()
            notice = str(pilot.app.query_one("#waiting-notice").content)
            # The bus flag must have been released so the next keypress
            # can take effect.
            idle = not pilot.app._busy  # noqa: SLF001
            return notice, idle

    notice, idle = asyncio.run(scenario())
    assert "database is locked" in notice
    assert idle, "action handler must not leave the TUI in a busy state on DB error"


def test_approve_action_swallows_db_error_from_approve_one(repos: Repos) -> None:
    """issue #12 (extended): ``_approve`` runs ``approve_one`` on a worker
    thread. If the DB throws there, the await propagates the exception
    into the event loop -- Textual swallows it and prints the traceback.
    The async wrapper must catch and render the error inline."""

    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command="uptime",
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )

    async def scenario() -> tuple[str, bool]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with patch(
                "cgate.watch.app.mark_approved",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                await pilot.press("y")
                await pilot.pause()
            notice = str(pilot.app.query_one("#waiting-notice").content)
            idle = not pilot.app._busy  # noqa: SLF001
            return notice, idle

    notice, idle = asyncio.run(scenario())
    assert "database is locked" in notice
    assert idle, "_approve must release _busy even when approve_one raises sqlite3.Error"


def test_approve_action_swallows_connection_not_found_from_approve_one(repos: Repos) -> None:
    """A connection removed after its command was queued makes mark_approved
    raise ConnectionNotFoundError -- not a sqlite3.Error. Uncaught, that
    would crash the whole dashboard over one bad command instead of just
    that one approval."""
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command="uptime",
    )
    # No matching connection is registered, so mark_approved raises
    # ConnectionNotFoundError before anything reaches the executor.

    async def scenario() -> tuple[str, bool]:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            notice = str(pilot.app.query_one("#waiting-notice").content)
            idle = not pilot.app._busy  # noqa: SLF001
            return notice, idle

    notice, idle = asyncio.run(scenario())
    assert "linux-1" in notice
    assert idle, "_approve must release _busy even when approve_one raises ConnectionNotFoundError"


def test_db_error_with_brackets_in_message_does_not_crash_tui(repos: Repos) -> None:
    """Regression for issue #39: a sqlite3.Error whose ``str()`` contains
    ``[`` or ``]`` used to crash ``_render_db_error`` via ``Static.update``
    → ``rich.errors.MarkupError`` (the same class #33 fixed for the
    list-row sites). The error banner must now escape the exception text
    before interpolating it into markup.
    """
    bracketed_msg = "table [secret] is missing"
    exc = sqlite3.OperationalError(bracketed_msg)

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            with patch("cgate.watch.app.count_waiting", side_effect=exc):
                pilot.app._refresh()  # noqa: SLF001
                await pilot.pause()
            return str(pilot.app.query_one("#waiting-notice").content)

    notice = asyncio.run(scenario())
    # The brackets must reach the rendered notice literally, escaped -- so
    # Textual's parser doesn't try to interpret them as a tag.
    assert "\\[secret\\]" in notice
    assert "[secret]" not in notice


def test_approval_error_with_brackets_in_connection_alias_does_not_crash_tui(repos: Repos) -> None:
    """Regression for issue #39: a connection alias containing ``[`` or
    ``]`` -- reachable through any source that can write to the connections
    table, including manual SQL -- triggers ``ConnectionNotFoundError`` with
    the alias embedded in the message. ``_render_approval_error`` must
    escape the exception text, otherwise the TUI crashes the same way
    issue #33 crashed the list rows."""
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    _ = repos.commands.add(
        batch_id=lot.id,
        server_alias="srv[prod]",
        server_type=ServerType.WINDOWS,
        command="uptime",
    )
    # No matching connection is registered, so mark_approved raises
    # ConnectionNotFoundError("srv[prod]") before reaching the executor.

    async def scenario() -> str:
        async with _app(repos).run_test() as pilot:
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            return str(pilot.app.query_one("#waiting-notice").content)

    notice = asyncio.run(scenario())
    # The bracket-bearing alias must reach the notice escaped.
    assert "srv\\[prod\\]" in notice
    assert "srv[prod]" not in notice


# ---------------------------------------------------------------------------
# Regression tests for issue #36 -- the EXECUTING CAS state machine that
# prevents ``execute_and_finalize`` from running an approved command twice.
# See src/cgate/watch/approval.py.
# ---------------------------------------------------------------------------


from datetime import UTC, datetime, timedelta  # noqa: E402  -- grouped with the #36/#38 tests below


def test_execute_and_finalize_cas_marks_row_executing_with_claim_timestamp(
    repos: Repos,
) -> None:
    """issue #36: ``execute_and_finalize`` must CAS APPROVED -> EXECUTING
    with ``claimed_at`` set to the current time before invoking the
    remote executor. Without the CAS, two callers observing status ==
    APPROVED could both run the destructive command twice (the original
    bug). ``claimed_at`` is the heal's liveness marker (issue #38).
    """
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)

    with patch("cgate.executor.selector.execute_command", return_value=_success()):
        updated, _ = execute_and_finalize(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    assert updated is not None
    assert updated.status is CommandStatus.EXECUTED
    # The CAS path stamps claimed_at on the way to EXECUTING; it is
    # preserved on the terminal write so the audit trail shows when the
    # executor claimed the command.
    assert updated.claimed_at is not None


def test_execute_and_finalize_backs_off_when_row_already_executing(repos: Repos) -> None:
    """issue #36: if another caller already CAS'd APPROVED -> EXECUTING,
    the second caller's CAS must lose and ``execute_and_finalize`` must
    return without invoking the executor or writing a terminal status.
    This is the core race the CAS is preventing -- without it, two
    observers of APPROVED would both execute the remote command.
    """
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)
    # Simulate the winner: another process has already CAS'd the row to
    # EXECUTING (with a fresh claimed_at -- the live process).
    _ = repos.commands.update_status(
        cmd.id,
        status=CommandStatus.EXECUTING,
        claimed_at=datetime.now(UTC),
        expected_status=CommandStatus.APPROVED,
    )

    with patch("cgate.executor.selector.execute_command") as execute:
        updated, result = execute_and_finalize(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    # Must NOT have invoked the executor -- the CAS must lose before the
    # remote call.
    execute.assert_not_called()
    # Returns the command as-is (still EXECUTING); no ExecutionResult.
    assert updated is not None
    assert updated.status is CommandStatus.EXECUTING
    assert result is None


def test_two_concurrent_execute_and_finalize_invoke_executor_at_most_once(
    repos: Repos,
) -> None:
    """issue #36 end-to-end: two callers race ``execute_and_finalize`` on
    the same APPROVED command. Only one of them must actually invoke
    ``execute_command`` -- the other backs off at the CAS. This is the
    reproduction from the issue body (two ``cgate watch`` instances, or
    the TUI + the MCP AUTO-mode auto-execution path, observing APPROVED
    at the same instant).

    Uses two threads with a barrier on the CAS path so both threads
    have read APPROVED before either writes the CAS -- forces the race
    window deterministically. Without the barrier the GIL + SQLite's
    busy-timeout would still serialize, but the window is so tight
    that CI noise could mask a regression.
    """
    threads_per_test = 2
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)

    call_count_lock = threading.Lock()
    call_count = 0
    cas_started = threading.Barrier(threads_per_test)
    both_in_cas = threading.Barrier(threads_per_test)

    real_update_status = repos.commands.update_status

    def slow_cas_then_normal(*args: object, **kwargs: object) -> bool:
        nonlocal call_count
        # We only synchronize on the EXECUTING CAS path. The other calls
        # (PENDING -> APPROVED before the race, the terminal EXECUTED write
        # after) run straight through.
        if kwargs.get("status") is CommandStatus.EXECUTING:
            with call_count_lock:
                call_count += 1
            # Both threads reach the CAS at roughly the same time; let
            # them both enter, then release so both attempt the SQL write.
            cas_started.wait()
            both_in_cas.wait()
        return real_update_status(*args, **kwargs)

    def runner() -> None:
        with (
            patch("cgate.executor.selector.execute_command", return_value=_success()),
            patch.object(
                repos.commands,
                "update_status",
                side_effect=slow_cas_then_normal,
            ),
        ):
            execute_and_finalize(
                db=repos.db,
                commands=repos.commands,
                connections=repos.connections,
                    batches=repos.batches,
                    command_id=cmd.id,
                )

    threads = [threading.Thread(target=runner) for _ in range(threads_per_test)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    for index, thread in enumerate(threads, start=1):
        assert not thread.is_alive(), f"thread {index} deadlocked"

    # Both threads reached the CAS -- if they didn't, the barrier test is
    # not actually exercising the race.
    assert call_count == threads_per_test, (
        f"expected both threads to attempt the CAS, got {call_count} "
        "attempts -- the test cannot prove anything without one that already lost"
    )
    # And the row must be in a terminal state (the winner stamped it).
    final = repos.commands.get(cmd.id)
    assert final is not None
    assert final.status is CommandStatus.EXECUTED, (
        "the CAS winner must have stamped the terminal status"
    )


# ---------------------------------------------------------------------------
# Regression tests for issue #38 -- the heal distinguishes live EXECUTING
# commands (process mid-call, fresh claimed_at) from dead ones
# (process died mid-call, stale claimed_at). The mixed test below
# (#38 follow-up) also verifies the executor's terminal write uses
# expected_status=EXECUTING so the heal's FAILED stamp survives a late
# result from the original (zombie) executor.
# See src/cgate/watch/queue.py and src/cgate/watch/approval.py.
# ---------------------------------------------------------------------------


def test_executor_terminal_write_is_a_noop_when_heal_already_marked_failed(
    repos: Repos,
) -> None:
    """issue #38 follow-up: the heal marks a stuck EXECUTING command
    FAILED on the next launch. If the original executor's network call
    later returns successfully and tries to stamp EXECUTED, that write
    must lose the CAS (because status is no longer EXECUTING -- it is
    FAILED from the heal). Without the CAS guard, the heal's FAILED
    stamp would be overwritten with EXECUTED, hiding the crash from the
    audit trail.
    """

    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)
    # Process claims EXECUTING with a stale claim, then dies.
    _ = repos.commands.update_status(
        cmd.id,
        status=CommandStatus.EXECUTING,
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
        expected_status=CommandStatus.APPROVED,
    )

    # Heal runs on the next launch and marks the orphan FAILED.
    fail_orphaned_approvals(repos.commands)
    after_heal = repos.commands.get(cmd.id)
    assert after_heal is not None
    assert after_heal.status is CommandStatus.FAILED

    # The original (zombie) executor's network call now returns and
    # tries to write the success result. The CAS must lose.
    with patch("cgate.executor.selector.execute_command", return_value=_success()):
        updated, _ = execute_and_finalize(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    # Status remains FAILED -- the heal's stamp survives. The executor
    # returns the (now stale) command unchanged.
    final = repos.commands.get(cmd.id)
    assert final is not None
    assert final.status is CommandStatus.FAILED, (
        "the executor's terminal write clobbered the heal's FAILED stamp -- "
        "the CAS guard on EXECUTING -> terminal must hold"
    )
    assert updated is not None
    assert updated.status is CommandStatus.FAILED


def test_executor_exception_marks_command_failed_with_expected_status_guard(
    repos: Repos,
) -> None:
    """issue #36 follow-up #4: if ``execute_command`` raises (SSH
    transport failure, WinRM HTTP error, connection refused, ...), the row
    would otherwise sit in ``EXECUTING`` until the next heal -- and the
    batch would block the FIFO queue while ``resolve_stale_batches`` waits
    for every command to terminate. ``execute_and_finalize`` must catch
    the exception, mark FAILED with the ``expected_status=EXECUTING``
    CAS guard, and return cleanly (no re-raise) so the caller doesn't
    need a second try/except layer to handle the failure path.
    """
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)

    class FakeConnectionError(RuntimeError):
        """Stand-in for SSH / WinRM transport failures the executor surfaces."""

    _err = "connection refused"

    def fake_execute(*_args: object, **_kwargs: object) -> object:
        raise FakeConnectionError(_err)

    with patch("cgate.executor.selector.execute_command", side_effect=fake_execute):
        updated, result = execute_and_finalize(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    # Must NOT re-raise -- caller gets a clean (Command, None) return.
    assert result is None
    assert updated is not None
    assert updated.status is CommandStatus.FAILED, (
        "execute_command raised but the row was left in EXECUTING -- "
        "the batch will sit stuck in the FIFO queue until the next heal."
    )
    assert updated.result is not None
    assert "FakeConnectionError" in updated.result, (
        "the failure reason should be in the audit trail"
    )
    assert "connection refused" in updated.result


def test_heal_leaves_live_executing_command_alone(repos: Repos) -> None:
    """issue #38 core: a row in EXECUTING with a fresh claimed_at is a
    live executor process mid-call. The heal must NOT touch it. The
    pre-fix code would mark it FAILED and clobber the audit trail while
    the executor was still running.
    """

    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)
    # Live process: claimed just now, presumably mid-execute_command call.
    _ = repos.commands.update_status(
        cmd.id,
        status=CommandStatus.EXECUTING,
        claimed_at=datetime.now(UTC),
        expected_status=CommandStatus.APPROVED,
    )

    fail_orphaned_approvals(repos.commands)

    after = repos.commands.get(cmd.id)
    assert after is not None
    assert after.status is CommandStatus.EXECUTING, (
        "heal marked a live EXECUTING command FAILED -- "
        "this is exactly what issue #38 says must not happen"
    )
    assert after.claimed_at is not None


def test_heal_marks_stuck_executing_command_failed(repos: Repos) -> None:
    """issue #38: a row in EXECUTING with a stale claimed_at means the
    executor process died mid-call. The heal must mark it FAILED so the
    batch does not sit stuck in EXECUTING forever.
    """

    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    # Go through the proper state transitions: PENDING -> APPROVED ->
    # EXECUTING (with a stale claimed_at to simulate a dead executor).
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)
    _ = repos.commands.update_status(
        cmd.id,
        status=CommandStatus.EXECUTING,
        claimed_at=datetime.now(UTC) - timedelta(hours=1),
        expected_status=CommandStatus.APPROVED,
    )

    fail_orphaned_approvals(repos.commands)

    after = repos.commands.get(cmd.id)
    assert after is not None
    assert after.status is CommandStatus.FAILED
    assert after.result is not None
    assert "interrupted before completion" in after.result


def test_heal_keeps_existing_approved_behavior(repos: Repos) -> None:
    """Regression guard for the heal: an APPROVED command (the legacy
    orphan case the heal always handled) must still be marked FAILED.
    Adding the EXECUTING branch in #38 must not have regressed the
    pre-existing APPROVED handling.
    """

    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)

    fail_orphaned_approvals(repos.commands)

    after = repos.commands.get(cmd.id)
    assert after is not None
    assert after.status is CommandStatus.FAILED
    assert "interrupted before completion" in (after.result or "")


def test_heal_stuck_threshold_is_configurable(repos: Repos) -> None:
    """issue #38 follow-up: the stuck threshold must be configurable so
    operators with legitimately slow remote commands can extend it.
    Default is 120s; passing a tighter one (e.g. 1s) with a 5-second-old
    claim must classify the command as stuck.
    """

    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)
    _ = repos.commands.update_status(
        cmd.id,
        status=CommandStatus.EXECUTING,
        claimed_at=datetime.now(UTC) - timedelta(seconds=5),
        expected_status=CommandStatus.APPROVED,
    )

    fail_orphaned_approvals(
        repos.commands,
        stuck_threshold_seconds=1,
        executor_timeout_seconds=0,
    )

    after = repos.commands.get(cmd.id)
    assert after is not None
    assert after.status is CommandStatus.FAILED


def test_terminal_cas_loss_is_logged_and_keeps_heal_stamp(
    repos: Repos, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """issue #38 follow-up: the heal runs *between* the executor's CAS claim
    and its terminal write (the genuine zombie-executor race). The terminal
    write must lose the CAS, the heal's FAILED stamp must survive, and the
    lost result must be logged so the operator can investigate."""
    monkeypatch.setattr("cgate.core.paths.data_dir", lambda: tmp_path)
    lot = repos.batches.create(title="lot", description=None, requested_by_agent=None)
    cmd = repos.commands.add(
        batch_id=lot.id, server_alias="linux-1", server_type=ServerType.LINUX, command="uptime"
    )
    _ = repos.connections.add(
        alias="linux-1",
        hostname="linux.example",
        server_type=ServerType.LINUX,
        detection_ssh=True,
        detection_winrm=False,
    )
    _ = repos.commands.update_status(cmd.id, status=CommandStatus.APPROVED)

    def fake_execute(*_args: object, **_kwargs: object) -> ExecutionResult:
        # Mid-call: a heal with a negative threshold treats the live claim as stuck.
        fail_orphaned_approvals(
            repos.commands, stuck_threshold_seconds=-1, executor_timeout_seconds=0
        )
        return _success()

    with patch("cgate.executor.selector.execute_command", side_effect=fake_execute):
        updated, result = execute_and_finalize(
            db=repos.db,
            commands=repos.commands,
            connections=repos.connections,
            batches=repos.batches,
            command_id=cmd.id,
        )

    assert result is not None
    assert updated is not None
    assert updated.status is CommandStatus.FAILED
    assert updated.result is not None
    assert "interrupted before completion" in updated.result
    log_text = (tmp_path / "update.log").read_text(encoding="utf-8")
    assert str(cmd.id) in log_text
    assert "was not persisted" in log_text

"""Full-screen Textual dashboard for `cgate watch` (spec §Approval queue).

Replaces the earlier linear y/n/a/r prompt with a live view: a sidebar shows
the FIFO batch queue, the main panel shows the active batch's commands, and
approvals run in a worker thread so the UI stays responsive during exec.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, ListItem, ListView, Static
from typing_extensions import override

from cgate import __version__
from cgate.db.mode import AppModeNotSetError, Mode
from cgate.markup import escape as escape_markup
from cgate.watch.approval import (
    CommandDisappearedError,
    ConnectionNotFoundError,
    execute_and_finalize,
    mark_approved,
    reject_one,
)
from cgate.watch.command_detail_modal import CommandDetailModal
from cgate.watch.history_modal import HistoryModal
from cgate.watch.mode_modal import ModeModal, mode_markup
from cgate.watch.queue import (
    count_pending_commands,
    count_waiting,
    pending_commands_in_batch,
    select_active_batch,
)
from cgate.watch.render import format_batch_header, format_queue_summary, server_badge
from cgate.watch.server_settings_modal import ServerSettingsModal
from cgate.watch.theme import CGATE_THEME
from cgate.watch.widgets import CommandRow

if TYPE_CHECKING:
    from textual.binding import BindingType

    from cgate.connections.store import ConnectionsRepo
    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.connection import Database
    from cgate.db.mode import AppModeRepo
    from cgate.db.server_settings import ServerSettingsRepo
    from cgate.db.types import Batch, BatchId, Command, CommandId, Connection

_POLL_INTERVAL_SECONDS: float = 1.5


class BatchRow(ListItem):
    """One pending batch in the queue sidebar, carrying its id for selection."""

    batch_id: BatchId

    def __init__(self, batch: Batch, *, active: bool) -> None:
        """Render the marker/title and remember which batch this row is."""
        marker = "▶" if active else " "
        style = "bold" if active else "dim"
        super().__init__(
            Static(f"{marker} [{style}]{escape_markup(batch.title)}[/{style}]", markup=True)
        )
        self.batch_id = batch.id


class QueueSidebar(Vertical):
    """Left rail listing pending batches, FIFO order, active one marked.

    Enter on a highlighted row pins that batch as active regardless of
    FIFO order (`WatchApp.on_list_view_selected`), so a human can jump
    the queue and prioritize approving something further down.
    """

    _rendered: tuple[tuple[BatchId, str, bool], ...] = ()

    @override
    def compose(self) -> ComposeResult:
        """Build the static title, a usage hint, and the batch list view."""
        yield Static("[bold]Queue[/bold]", classes="sidebar-title")
        yield Static("[dim]↑/↓ Enter = jump to a batch[/dim]", classes="sidebar-hint")
        yield ListView(id="queue-list")

    def refresh_queue(self, pending: list[Batch], active_id: BatchId | None) -> None:
        """Rebuild the list view, keeping the same row highlighted if it still exists.

        Runs on every poll tick (every `_POLL_INTERVAL_SECONDS`), not just
        on user action. Two things follow from that:

        - It must not reset a human's cursor mid-navigation -- restoring
          the highlighted batch by id (falling back to the first row)
          keeps ↑/↓ usable even while the queue is refreshing live.
        - The overwhelmingly common tick is "nothing changed"; clearing
          and rebuilding the list view anyway made every row visibly
          flash every `_POLL_INTERVAL_SECONDS` for no reason. Skipping
          the rebuild whenever the rendered snapshot is identical fixes
          that without touching the polling itself.
        """
        snapshot = tuple((batch.id, batch.title, batch.id == active_id) for batch in pending)
        if snapshot == self._rendered:
            return
        self._rendered = snapshot
        list_view = self.query_one("#queue-list", ListView)
        highlighted = list_view.highlighted_child
        highlighted_id = highlighted.batch_id if isinstance(highlighted, BatchRow) else None
        _ = list_view.clear()
        for batch in pending:
            _ = list_view.append(BatchRow(batch, active=batch.id == active_id))
        if pending:
            restore_index = next(
                (i for i, batch in enumerate(pending) if batch.id == highlighted_id), 0
            )
            list_view.index = restore_index


class ModeHeader(Horizontal):
    """Top bar: app title on the left, global mode indicator on the right."""

    @override
    def compose(self) -> ComposeResult:
        """Build the title static and the right-aligned mode indicator."""
        yield Static(f"🔐 [bold]cgate watch[/bold]  [dim]v{__version__}[/dim]", id="mode-title")
        yield Static(id="mode-indicator")

    def show_mode(self, mode: Mode, *, auto_allowed: int, total: int) -> None:
        """Render the mode label; only AUTO also shows the auto-allowed count."""
        text = f"MODE: {mode_markup(mode)}"
        if mode is Mode.AUTO:
            text += f" [dim]|[/dim] {auto_allowed} servers auto-allowed (of {total})"
        _ = self.query_one("#mode-indicator", Static).update(text)


class ServersSidebar(Vertical):
    """Left-rail section listing every connection with its auto-approve flag."""

    _rendered: tuple[tuple[str, bool], ...] = ()

    @override
    def compose(self) -> ComposeResult:
        """Build the static title and the servers list view."""
        yield Static("[bold]Servers[/bold]", classes="sidebar-title")
        yield ListView(id="servers-list")

    def refresh_servers(self, rows: list[tuple[Connection, bool]]) -> None:
        """Replace the list view contents with the current connections and flags.

        Skips the rebuild when nothing changed since the last poll tick --
        see `QueueSidebar.refresh_queue` for why that matters.
        """
        snapshot = tuple((connection.alias, auto_allowed) for connection, auto_allowed in rows)
        if snapshot == self._rendered:
            return
        self._rendered = snapshot
        list_view = self.query_one("#servers-list", ListView)
        _ = list_view.clear()
        for connection, auto_allowed in rows:
            checkbox = "[✓]" if auto_allowed else "[ ]"
            badge = server_badge(connection.server_type)
            _ = list_view.append(
                ListItem(
                    Static(
                        f"{escape_markup(connection.alias)}  {badge}  {checkbox}",
                        markup=True,
                    )
                )
            )


class ActivePanel(Vertical):
    """Main pane: the active batch's header and its command rows.

    Enter on a highlighted command row opens its full-detail modal
    (`WatchApp.on_list_view_selected`) -- the untruncated result, the
    agent's `reason`, and who/what approved it.
    """

    _shown_batch_id: BatchId | None = None

    @override
    def compose(self) -> ComposeResult:
        """Build the header static, a usage hint, and the row list view."""
        yield Static(id="active-header")
        yield Static("[dim]Enter on a command = view full result[/dim]", classes="sidebar-hint")
        yield ListView(id="rows")

    def show_idle(self) -> None:
        """Show the empty-queue placeholder and drop any stale rows."""
        self._shown_batch_id = None
        _ = self.query_one("#active-header", Static).update(
            "[dim]No pending batches — waiting for new proposals…[/dim]"
        )
        _ = self.query_one("#rows", ListView).clear()

    def show_batch(self, batch: Batch, commands_in_batch: list[Command]) -> None:
        """Render one batch's header, updating existing rows in place when possible."""
        _ = self.query_one("#active-header", Static).update(format_batch_header(batch))
        rows = self.query_one("#rows", ListView)
        if batch.id != self._shown_batch_id:
            self._shown_batch_id = batch.id
            _ = rows.clear()
            for command in commands_in_batch:
                _ = rows.append(ListItem(CommandRow(command)))
            if commands_in_batch:
                rows.index = 0
            return
        existing = {row.command_id: row for row in rows.query(CommandRow)}
        for command in commands_in_batch:
            row = existing.get(command.id)
            if row is not None:
                row.update_command(command)
            else:
                _ = rows.append(ListItem(CommandRow(command)))


class WatchApp(App[None]):
    """Approval dashboard: sidebar queue + active-batch panel, keys y/n/a/r/h/q."""

    CSS: ClassVar[str] = """
    Screen { background: $surface; }
    ModeHeader { dock: top; height: 1; background: $panel; padding: 0 1; }
    #mode-title { width: auto; color: $primary; }
    #mode-indicator { width: 1fr; text-align: right; }
    #body { height: 1fr; }
    #sidebar { width: 36; border-right: solid $panel; }
    QueueSidebar { padding: 1; height: 1fr; }
    ServersSidebar { padding: 1; height: auto; max-height: 45%; border-top: solid $panel; }
    .sidebar-title { margin-bottom: 1; }
    .sidebar-hint { margin-bottom: 1; }
    ActivePanel { padding: 1 2; }
    #active-header { margin-bottom: 1; }
    #rows { height: 1fr; }
    CommandRow { margin-bottom: 1; }
    #waiting-notice { padding: 0 2; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "approve_one", "Approve"),
        Binding("n", "reject_one", "Reject"),
        Binding("a", "approve_all", "Approve all"),
        Binding("r", "reject_all", "Reject all"),
        Binding("m", "toggle_mode", "Mode"),
        Binding("s", "server_settings", "Servers"),
        Binding("h", "history", "History"),
        Binding("q", "quit", "Quit"),
    ]

    _db: Database  # class-level annotation required by strict mode
    _batches: BatchesRepo  # class-level annotation required by strict mode
    _commands: CommandsRepo  # class-level annotation required by strict mode
    _connections: ConnectionsRepo  # class-level annotation required by strict mode
    _mode: AppModeRepo  # class-level annotation required by strict mode
    _server_settings: ServerSettingsRepo  # class-level annotation required by strict mode
    _busy: bool
    _pinned_batch_id: BatchId | None

    def __init__(  # noqa: PLR0913 - signature follows the required repository DI boundary
        self,
        *,
        db: Database,
        batches: BatchesRepo,
        commands: CommandsRepo,
        connections: ConnectionsRepo,
        mode: AppModeRepo,
        server_settings: ServerSettingsRepo,
    ) -> None:
        """Store the repository collaborators used to read and mutate the queue."""
        super().__init__()
        self.register_theme(CGATE_THEME)
        self.theme = CGATE_THEME.name  # pyright: ignore[reportUnannotatedClassAttribute]
        self._db = db
        self._batches = batches
        self._commands = commands
        self._connections = connections
        self._mode = mode
        self._server_settings = server_settings
        self._busy = False
        self._pinned_batch_id = None

    @override
    def compose(self) -> ComposeResult:
        """Lay out mode header, waiting notice, sidebar + active panel, and footer."""
        yield ModeHeader()
        yield Static(id="waiting-notice")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield QueueSidebar()
                yield ServersSidebar()
            yield ActivePanel()
        yield Footer()

    def on_mount(self) -> None:
        """Render the initial state and start polling for external queue changes."""
        self._refresh()
        _ = self.set_interval(_POLL_INTERVAL_SECONDS, self._refresh)

    def _render_notice(self, text: str, sub_title: str) -> None:
        """Update the waiting-notice and sub_title without ever raising.

        Best-effort: if the widget tree is already gone (e.g. `query_one`
        raises during teardown), this swallows rather than raising, since
        Textual would otherwise print its own traceback on top of the one
        being reported.
        """
        try:
            notice = self.query_one("#waiting-notice", Static)
            _ = notice.update(text)
            self.sub_title = sub_title  # pyright: ignore[reportUnannotatedClassAttribute]
        except Exception:
            pass

    def _render_db_error(self, exc: sqlite3.Error) -> None:
        """Render a database error on the TUI without raising (issue #12).

        Textual's widget-exception handler runs `_handle_exception` →
        `_fatal_error` → prints a raw
        `rich.traceback.Traceback(show_locals=True)` on `_shutdown()`,
        bypassing the `sqlite3.Error` boundary in `cli/main.py`. Every
        DB-touching method on this app must funnel errors through this
        helper so the user sees a clean in-UI message and stays in the
        dashboard; the next polling tick (or the user's next keypress)
        will retry the read automatically.
        """
        error = CGATE_THEME.error
        self._render_notice(
            (
                f"[{error}]Could not read the database:[/{error}] {exc}\n"
                "[dim]Check that no other cgate process is locking "
                "cgate.db. The next read will retry automatically.[/dim]"
            ),
            "database error",
        )

    def _render_approval_error(self, exc: Exception) -> None:
        """Render an approval failure on the TUI without raising or crashing.

        `approve_one` can also raise `ConnectionNotFoundError` (the
        connection was removed after the command was queued) or
        `CommandDisappearedError`, on top of `sqlite3.Error`. Left
        uncaught inside a Textual action, either would crash the whole
        dashboard the same way issue #12 did -- over a single bad
        command. The busy flag is still released by `_approve`'s
        `finally`, so the rest of the queue stays fully usable.
        """
        error = CGATE_THEME.error
        self._render_notice(
            f"[{error}]Could not approve this command:[/{error}] {exc}", "approval error"
        )

    def _global_mode(self) -> Mode:
        """Return the persisted global mode, defaulting to PROPOSE when unset."""
        try:
            return self._mode.get().mode
        except AppModeNotSetError:
            return Mode.PROPOSE

    def _refresh_mode_and_servers(self) -> None:
        """Update the mode header and the servers sidebar from the database."""
        mode = self._global_mode()
        connections = self._connections.list_all()
        rows = [
            (connection, self._server_settings.get_or_default(connection.alias).auto_allowed)
            for connection in connections
        ]
        auto_allowed = sum(1 for _, allowed in rows if allowed)
        self.query_one(ModeHeader).show_mode(mode, auto_allowed=auto_allowed, total=len(rows))
        self.query_one(ServersSidebar).refresh_servers(rows)

    def _refresh(self) -> None:
        """Re-read the queue from the database and update every widget from it."""
        try:
            self._refresh_mode_and_servers()
            pending = self._batches.list_pending()
            active = select_active_batch(pending, self._pinned_batch_id)
            self.query_one(QueueSidebar).refresh_queue(pending, active.id if active else None)
            pending_total = count_pending_commands(pending, self._commands)
            notice = self.query_one("#waiting-notice", Static)
            _ = notice.update(
                format_queue_summary(
                    pending_commands=pending_total,
                    waiting_batches=count_waiting(self._batches),
                )
            )
            panel = self.query_one(ActivePanel)
            if active is None:
                panel.show_idle()
                # Reactive[str] on the base class; reassigning it is the documented
                # Textual pattern, but basedpyright wants a same-interval annotation.
                self.sub_title = "no pending batches"  # pyright: ignore[reportUnannotatedClassAttribute]
                return
            commands_in_batch = self._commands.list_for_batch(active.id)
            panel.show_batch(active, commands_in_batch)
            pending_here = len(pending_commands_in_batch(commands_in_batch))
            self.sub_title = f"{pending_here} pending"
        except sqlite3.Error as exc:
            self._render_db_error(exc)

    def _first_pending(self) -> Command | None:
        """Return the active batch's next pending command, or None.

        "Active" honors a pinned batch (`select_active_batch`) the same
        way `_refresh` does, so y/n/a/r act on whatever the human picked
        in the queue sidebar rather than always the FIFO-oldest one.

        Returns ``None`` on a DB error too -- action handlers already
        treat ``None`` as "no-op", so the user's keypress becomes a
        silent skip while the error message stays on screen.
        """
        try:
            pending = self._batches.list_pending()
            active = select_active_batch(pending, self._pinned_batch_id)
            if active is None:
                return None
            commands_in_batch = self._commands.list_for_batch(active.id)
            remaining = pending_commands_in_batch(commands_in_batch)
            return remaining[0] if remaining else None
        except sqlite3.Error as exc:
            self._render_db_error(exc)
            return None

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Pin a batch chosen in the queue sidebar, or open a command's full detail."""
        if event.list_view.id == "queue-list" and isinstance(event.item, BatchRow):
            self._pinned_batch_id = event.item.batch_id
            self._refresh()
        elif event.list_view.id == "rows":
            row = event.item.query_one(CommandRow)
            self._show_command_detail(row.command_id)

    def _show_command_detail(self, command_id: CommandId) -> None:
        """Open the full-detail modal for one command; best-effort on a DB error."""
        try:
            command = self._commands.get(command_id)
        except sqlite3.Error as exc:
            self._render_db_error(exc)
            return
        if command is not None:
            _ = self.push_screen(CommandDetailModal(command))

    async def action_approve_one(self) -> None:
        """Approve and execute the active batch's next pending command."""
        if self._busy:
            return
        command = self._first_pending()
        if command is None:
            return
        await self._approve(command.id)

    def action_reject_one(self) -> None:
        """Reject the active batch's next pending command."""
        if self._busy:
            return
        command = self._first_pending()
        if command is None:
            return
        try:
            _ = reject_one(commands=self._commands, batches=self._batches, command_id=command.id)
        except sqlite3.Error as exc:
            self._render_db_error(exc)
            return
        self._refresh()

    async def action_approve_all(self) -> None:
        """Approve and execute every pending command in the active batch, in order."""
        if self._busy:
            return
        while (command := self._first_pending()) is not None:
            await self._approve(command.id)

    def action_reject_all(self) -> None:
        """Reject every pending command in the active batch."""
        if self._busy:
            return
        while (command := self._first_pending()) is not None:
            try:
                _ = reject_one(
                    commands=self._commands,
                    batches=self._batches,
                    command_id=command.id,
                )
            except sqlite3.Error as exc:
                self._render_db_error(exc)
                return
        self._refresh()

    def action_toggle_mode(self) -> None:
        """Open the confirmation modal and flip the global mode on confirm."""

        def _after(confirmed: bool | None) -> None:  # noqa: FBT001 - Textual push_screen callback signature
            if confirmed:
                self._refresh()

        self.push_screen(ModeModal(mode_repo=self._mode), _after)

    def action_server_settings(self) -> None:
        """Open the per-server auto-approve editor and refresh on save."""

        def _after(saved: bool | None) -> None:  # noqa: FBT001 - Textual push_screen callback signature
            if saved:
                self._refresh()

        self.push_screen(
            ServerSettingsModal(
                connections=self._connections,
                settings=self._server_settings,
                mode_repo=self._mode,
            ),
            _after,
        )

    def action_history(self) -> None:
        """Open the read-only browser for resolved batches."""
        _ = self.push_screen(HistoryModal(batches=self._batches, commands=self._commands))

    async def _approve(self, command_id: CommandId) -> None:
        """Mark approved, refresh (so the queue shows it's running), then execute.

        Split into two off-thread calls instead of one so the human sees
        the "approved, running" state (status glyph ◐, plus the
        "running…" hint in `format_command_line`) the moment `y` is
        pressed -- not just silence for however long the executor's
        timeout allows, which reads as the dashboard having frozen.
        """
        self._busy = True
        try:
            try:
                _ = await asyncio.to_thread(
                    mark_approved,
                    commands=self._commands,
                    connections=self._connections,
                    command_id=command_id,
                )
            except sqlite3.Error as exc:
                self._render_db_error(exc)
                return
            except ConnectionNotFoundError as exc:
                self._render_approval_error(exc)
                return
            self._refresh()
            try:
                _ = await asyncio.to_thread(
                    execute_and_finalize,
                    db=self._db,
                    commands=self._commands,
                    connections=self._connections,
                    batches=self._batches,
                    command_id=command_id,
                )
            except sqlite3.Error as exc:
                self._render_db_error(exc)
                return
            except CommandDisappearedError as exc:
                self._render_approval_error(exc)
                return
        finally:
            self._busy = False
        self._refresh()


__all__ = ["WatchApp"]

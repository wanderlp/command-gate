"""Full-screen Textual dashboard for `cgate watch` (spec §Cola de aprobación).

Replaces the earlier linear y/n/a/r prompt with a live view: a sidebar shows
the FIFO batch queue, the main panel shows the active batch's commands, and
approvals run in a worker thread so the UI stays responsive during exec.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, ListItem, ListView, Static
from typing_extensions import override

from cgate.watch.approval import approve_one, reject_one
from cgate.watch.queue import count_waiting, pending_commands_in_batch
from cgate.watch.render import format_batch_header, format_command_line, format_waiting_notice

if TYPE_CHECKING:
    from textual.binding import BindingType

    from cgate.connections.store import ConnectionsRepo
    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.connection import Database
    from cgate.db.types import Batch, BatchId, Command, CommandId

_POLL_INTERVAL_SECONDS: float = 1.5


class QueueSidebar(Vertical):
    """Left rail listing pending batches, FIFO order, active one marked."""

    @override
    def compose(self) -> ComposeResult:
        """Build the static title and the batch list view."""
        yield Static("[bold]Cola[/bold]", classes="sidebar-title")
        yield ListView(id="queue-list")

    def refresh_queue(self, pending: list[Batch], active_id: BatchId | None) -> None:
        """Replace the list view contents with the current pending batches."""
        list_view = self.query_one("#queue-list", ListView)
        _ = list_view.clear()
        for batch in pending:
            marker = "▶" if batch.id == active_id else " "
            style = "bold cyan" if batch.id == active_id else "dim"
            _ = list_view.append(
                ListItem(Static(f"{marker} [{style}]{batch.title}[/{style}]", markup=True))
            )


class CommandRow(Static):
    """One command line inside the active-batch panel, updatable in place."""

    command_id: CommandId

    def __init__(self, command: Command) -> None:
        """Render the initial line for this command and remember its id."""
        super().__init__(format_command_line(command), markup=True)
        self.command_id = command.id

    def update_command(self, command: Command) -> None:
        """Refresh this row's text for the command's current state."""
        _ = self.update(format_command_line(command))


class ActivePanel(VerticalScroll):
    """Main pane: the active batch's header and its command rows."""

    _shown_batch_id: BatchId | None = None

    @override
    def compose(self) -> ComposeResult:
        """Build the header static and the row container."""
        yield Static(id="active-header")
        yield Vertical(id="rows")

    def show_idle(self) -> None:
        """Show the empty-queue placeholder and drop any stale rows."""
        self._shown_batch_id = None
        _ = self.query_one("#active-header", Static).update(
            "[dim]Sin lotes pendientes — esperando nuevas propuestas…[/dim]"
        )
        _ = self.query_one("#rows", Vertical).remove_children()

    def show_batch(self, batch: Batch, commands_in_batch: list[Command]) -> None:
        """Render one batch's header, updating existing rows in place when possible."""
        _ = self.query_one("#active-header", Static).update(format_batch_header(batch))
        rows = self.query_one("#rows", Vertical)
        if batch.id != self._shown_batch_id:
            self._shown_batch_id = batch.id
            _ = rows.remove_children()
            for command in commands_in_batch:
                _ = rows.mount(CommandRow(command))
            return
        existing = {row.command_id: row for row in rows.query(CommandRow)}
        for command in commands_in_batch:
            row = existing.get(command.id)
            if row is not None:
                row.update_command(command)
            else:
                _ = rows.mount(CommandRow(command))


class WatchApp(App[None]):
    """Approval dashboard: sidebar queue + active-batch panel, keys y/n/a/r/q."""

    CSS: ClassVar[str] = """
    Screen { background: $surface; }
    #body { height: 1fr; }
    QueueSidebar { width: 34; border-right: solid $panel; padding: 1; }
    .sidebar-title { margin-bottom: 1; }
    ActivePanel { padding: 1 2; }
    #active-header { margin-bottom: 1; }
    CommandRow { margin-bottom: 1; }
    #waiting-notice { padding: 0 2; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "approve_one", "Aprobar"),
        Binding("n", "reject_one", "Rechazar"),
        Binding("a", "approve_all", "Aprobar todo"),
        Binding("r", "reject_all", "Rechazar todo"),
        Binding("q", "quit", "Salir"),
    ]

    _db: Database  # class-level annotation required by strict mode
    _batches: BatchesRepo  # class-level annotation required by strict mode
    _commands: CommandsRepo  # class-level annotation required by strict mode
    _connections: ConnectionsRepo  # class-level annotation required by strict mode
    _busy: bool

    def __init__(
        self,
        *,
        db: Database,
        batches: BatchesRepo,
        commands: CommandsRepo,
        connections: ConnectionsRepo,
    ) -> None:
        """Store the repository collaborators used to read and mutate the queue."""
        super().__init__()
        self._db = db
        self._batches = batches
        self._commands = commands
        self._connections = connections
        self._busy = False

    @override
    def compose(self) -> ComposeResult:
        """Lay out header, waiting notice, sidebar + active panel, and footer."""
        yield Header(show_clock=True)
        yield Static(id="waiting-notice")
        with Horizontal(id="body"):
            yield QueueSidebar()
            yield ActivePanel()
        yield Footer()

    def on_mount(self) -> None:
        """Render the initial state and start polling for external queue changes."""
        self._refresh()
        _ = self.set_interval(_POLL_INTERVAL_SECONDS, self._refresh)

    def _refresh(self) -> None:
        """Re-read the queue from the database and update every widget from it."""
        pending = self._batches.list_pending()
        active = pending[0] if pending else None
        self.query_one(QueueSidebar).refresh_queue(pending, active.id if active else None)
        notice = self.query_one("#waiting-notice", Static)
        _ = notice.update(format_waiting_notice(count_waiting(self._batches)))
        panel = self.query_one(ActivePanel)
        if active is None:
            panel.show_idle()
            # Reactive[str] on the base class; reassigning it is the documented
            # Textual pattern, but basedpyright wants a same-scope annotation.
            self.sub_title = "sin lotes pendientes"  # pyright: ignore[reportUnannotatedClassAttribute]
            return
        commands_in_batch = self._commands.list_for_batch(active.id)
        panel.show_batch(active, commands_in_batch)
        pending_here = len(pending_commands_in_batch(commands_in_batch))
        self.sub_title = f"{pending_here} pendiente(s)"

    def _first_pending(self) -> Command | None:
        """Return the active batch's next pending command, or None."""
        pending = self._batches.list_pending()
        if not pending:
            return None
        commands_in_batch = self._commands.list_for_batch(pending[0].id)
        remaining = pending_commands_in_batch(commands_in_batch)
        return remaining[0] if remaining else None

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
        _ = reject_one(commands=self._commands, batches=self._batches, command_id=command.id)
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
            _ = reject_one(commands=self._commands, batches=self._batches, command_id=command.id)
        self._refresh()

    async def _approve(self, command_id: CommandId) -> None:
        """Run one approval off the event loop thread, then refresh the view."""
        self._busy = True
        try:
            _ = await asyncio.to_thread(
                approve_one,
                db=self._db,
                commands=self._commands,
                connections=self._connections,
                batches=self._batches,
                command_id=command_id,
            )
        finally:
            self._busy = False
        self._refresh()


__all__ = ["WatchApp"]

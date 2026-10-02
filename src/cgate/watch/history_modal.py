"""Read-only browser for resolved batches (`h` key in `cgate watch`).

The live queue drops a batch the instant it resolves -- `resolve_at IS
NULL` is exactly what makes it disappear from `list_pending`. Without
this, there was no way to look back at anything that already went
through: not in the TUI, not from any CLI command.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static
from typing_extensions import override

from cgate.watch.command_detail_modal import CommandDetailModal
from cgate.watch.render import format_batch_header
from cgate.watch.theme import CGATE_THEME
from cgate.watch.widgets import CommandRow
from cgate.markup import escape as escape_markup

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.types import Batch, BatchId

_HISTORY_LIMIT = 50


class BatchHistoryRow(ListItem):
    """One resolved batch in the history list, carrying its id for lookups."""

    batch_id: BatchId

    def __init__(self, batch: Batch) -> None:
        """Render the resolved timestamp and title, and remember the batch id."""
        resolved = batch.resolved_at.strftime("%Y-%m-%d %H:%M") if batch.resolved_at else "?"
        super().__init__(
            Static(
                f"[dim]{resolved}[/dim]  {escape_markup(batch.title)}",
                markup=True,
            )
        )
        self.batch_id = batch.id


class HistoryModal(ModalScreen[None]):
    """Browse resolved batches: title/timestamp list on the left, commands on the right.

    Press `/` to filter (fzf/vim-style): the list narrows live against
    every batch's title, description, and its commands' text/server alias.
    Escape exits filter mode first, then closes the modal on a second press.
    """

    CSS: ClassVar[str] = """
    HistoryModal { align: center middle; }
    #history-dialog {
        width: 96%;
        height: 90%;
        border: round $primary;
        background: $surface;
    }
    #history-title { padding: 1 2 0 2; }
    #history-hint { padding: 0 2 1 2; }
    #history-body { height: 1fr; }
    #history-list-pane { width: 40; border-right: solid $panel; padding: 1; }
    #history-filter { margin-bottom: 1; }
    #history-detail-pane { padding: 1 2; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", priority=True),
        # Not priority: a focused #history-filter Input must still receive a
        # literal "q" character while the human is typing a query.
        Binding("q", "close", "Close", show=False),
        Binding("slash", "search", "Filter", priority=True),
    ]

    _batches: BatchesRepo
    _commands: CommandsRepo
    _resolved: list[Batch]
    _visible: list[Batch]
    _search_text: dict[BatchId, str]

    def __init__(self, *, batches: BatchesRepo, commands: CommandsRepo) -> None:
        """Store the repositories used to list resolved batches and their commands."""
        super().__init__()
        self._batches = batches
        self._commands = commands
        self._resolved = []
        self._visible = []
        self._search_text = {}

    @override
    def compose(self) -> ComposeResult:
        """Lay out the title, the filter input, the resolved-batch list, and the detail pane."""
        with Vertical(id="history-dialog"):
            yield Static("[bold]History[/bold] [dim](most recently resolved first)[/dim]",
                          id="history-title")
            with Horizontal(id="history-body"):
                with Vertical(id="history-list-pane"):
                    yield Input(placeholder="/ to filter…", id="history-filter")
                    yield ListView(id="history-list")
                with VerticalScroll(id="history-detail-pane"):
                    yield Static(id="history-detail-header")
                    yield ListView(id="history-rows")
            yield Static(
                (
                    "[dim]↑/↓ = browse batches — Enter on a command = view full result "
                    "— / = filter — Esc = close[/dim]"
                ),
                id="history-hint",
            )

    def on_mount(self) -> None:
        """Load the resolved batches, index them for filtering, and show the newest."""
        try:
            self._resolved = self._batches.list_resolved(limit=_HISTORY_LIMIT)
        except sqlite3.Error as exc:
            error = CGATE_THEME.error
            self.query_one("#history-detail-header", Static).update(
                f"[{error}]Could not read history:[/{error}] {escape_markup(str(exc))}"
            )
            return
        self._search_text = {batch.id: self._index_batch(batch) for batch in self._resolved}
        self._visible = self._resolved
        self._rebuild_list()
        _ = self.query_one("#history-list", ListView).focus()

    def _index_batch(self, batch: Batch) -> str:
        """Build the lowercased blob `_apply_filter` matches a query against.

        Best-effort: a DB hiccup while indexing one batch's commands just
        means that batch won't match on its command/server text -- title
        and description still will -- rather than breaking the whole list.
        """
        parts = [batch.title, batch.description or ""]
        try:
            for command in self._commands.list_for_batch(batch.id):
                parts.append(command.command)
                parts.append(command.server_alias)
        except sqlite3.Error:
            pass
        return " ".join(parts).lower()

    def action_search(self) -> None:
        """Focus the filter input (`/`), fzf/vim-style."""
        _ = self.query_one("#history-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-filter the batch list live as the query changes."""
        if event.input.id != "history-filter":
            return
        self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter jumps focus to the (now-narrowed) results list."""
        if event.input.id != "history-filter":
            return
        _ = self.query_one("#history-list", ListView).focus()

    def _apply_filter(self, query: str) -> None:
        """Narrow `_visible` to batches whose index contains `query`, then re-render."""
        normalized = query.strip().lower()
        self._visible = (
            self._resolved
            if not normalized
            else [b for b in self._resolved if normalized in self._search_text.get(b.id, "")]
        )
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        """Redraw #history-list from `_visible` and show the first match's detail."""
        list_view = self.query_one("#history-list", ListView)
        _ = list_view.clear()
        for batch in self._visible:
            _ = list_view.append(BatchHistoryRow(batch))
        if self._visible:
            list_view.index = 0
            self._show_batch(self._visible[0])
            return
        message = "No resolved batches yet." if not self._resolved else "No matches."
        self.query_one("#history-detail-header", Static).update(f"[dim]{message}[/dim]")
        _ = self.query_one("#history-rows", ListView).clear()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Show the highlighted batch's commands as the sidebar cursor moves."""
        if event.list_view.id != "history-list" or event.item is None:
            return
        if isinstance(event.item, BatchHistoryRow):
            batch = next(
                (b for b in self._resolved if b.id == event.item.batch_id), None
            )
            if batch is not None:
                self._show_batch(batch)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open the full-detail modal for the command highlighted in the detail pane."""
        if event.list_view.id != "history-rows":
            return
        row = event.item.query_one(CommandRow)
        try:
            command = self._commands.get(row.command_id)
        except sqlite3.Error as exc:
            error = CGATE_THEME.error
            self.query_one("#history-detail-header", Static).update(
                f"[{error}]Could not read this command:[/{error}] {escape_markup(str(exc))}"
            )
            return
        if command is not None:
            _ = self.app.push_screen(CommandDetailModal(command))  # pyright: ignore[reportUnknownMemberType]

    def _show_batch(self, batch: Batch) -> None:
        """Render one resolved batch's header and its commands in the detail pane."""
        self.query_one("#history-detail-header", Static).update(format_batch_header(batch))
        rows = self.query_one("#history-rows", ListView)
        _ = rows.clear()
        try:
            commands_in_batch = self._commands.list_for_batch(batch.id)
        except sqlite3.Error as exc:
            error = CGATE_THEME.error
            self.query_one("#history-detail-header", Static).update(
                f"[{error}]Could not read this batch's commands:[/{error}] {escape_markup(str(exc))}"
            )
            return
        for command in commands_in_batch:
            _ = rows.append(ListItem(CommandRow(command)))
        if commands_in_batch:
            rows.index = 0

    def action_close(self) -> None:
        """Exit filter mode first if it's active; otherwise dismiss (read-only view)."""
        filter_input = self.query_one("#history-filter", Input)
        if self.focused is filter_input or filter_input.value:
            filter_input.value = ""
            _ = self.query_one("#history-list", ListView).focus()
            return
        _ = self.dismiss(None)


__all__ = ["HistoryModal"]

"""Modal for editing per-server auto-approve opt-in flags (`s` key in cgate watch)."""

from __future__ import annotations

import getpass
import sqlite3
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static
from typing_extensions import override

from cgate.db.mode import AppModeNotSetError, Mode
from cgate.markup import escape as escape_markup
from cgate.watch.mode_modal import mode_markup
from cgate.watch.render import server_badge
from cgate.watch.theme import CGATE_THEME

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from cgate.connections.store import ConnectionsRepo
    from cgate.db.mode import AppModeRepo
    from cgate.db.server_settings import ServerSettingsRepo
    from cgate.db.types import Connection


def _updated_by() -> str:
    """Return the OS username for the audit column, falling back to 'unknown'."""
    try:
        return getpass.getuser() or "unknown"
    except (KeyError, OSError):
        return "unknown"


def _row_markup(connection: Connection, *, auto_allowed: bool) -> str:
    """Render one row: alias, server-type badge, and the auto-allowed checkbox."""
    checkbox = "[✓]" if auto_allowed else "[ ]"
    return f"{escape_markup(connection.alias)}  {server_badge(connection.server_type)}  {checkbox}"


class ServerRow(ListItem):
    """One connection row in the settings list, carrying its alias for toggling."""

    alias: str
    _connection: Connection  # class-level annotation required by strict mode

    def __init__(self, connection: Connection, *, auto_allowed: bool) -> None:
        """Render the row and remember which connection it belongs to."""
        super().__init__(Static(_row_markup(connection, auto_allowed=auto_allowed), markup=True))
        self.alias = connection.alias
        self._connection = connection

    def set_auto_allowed(self, *, auto_allowed: bool) -> None:
        """Re-render the row's checkbox for the new in-memory state."""
        _ = self.query_one(Static).update(_row_markup(self._connection, auto_allowed=auto_allowed))


class ServerSettingsModal(ModalScreen[bool]):
    """Edit per-server auto-approve flags: Space toggles, Enter saves, Esc cancels.

    Dismisses with ``True`` after a successful commit (even when nothing
    changed); dismisses with ``False`` on Esc, with no writes at all.
    """

    CSS: ClassVar[str] = """
    ServerSettingsModal { align: center middle; }
    #settings-dialog {
        width: 72;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #settings-list { height: auto; max-height: 16; margin-top: 1; }
    #settings-error { color: $error; }
    """

    # Priority bindings: checked before the focused ListView's own bindings,
    # so Enter commits instead of triggering ListView's row-select.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "toggle_row", "Toggle", show=False, priority=True),
        Binding("enter", "commit", "Save", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    _connections: ConnectionsRepo  # class-level annotation required by strict mode
    _settings: ServerSettingsRepo  # class-level annotation required by strict mode
    _mode_repo: AppModeRepo  # class-level annotation required by strict mode
    _state: dict[str, bool]
    _initial: dict[str, bool]

    def __init__(
        self,
        *,
        connections: ConnectionsRepo,
        settings: ServerSettingsRepo,
        mode_repo: AppModeRepo,
    ) -> None:
        """Store the repositories used to list connections and persist flag changes."""
        super().__init__()
        self._connections = connections
        self._settings = settings
        self._mode_repo = mode_repo
        self._state = {}
        self._initial = {}

    @override
    def compose(self) -> ComposeResult:
        """Show the title, the current global mode, the hint line, and the list."""
        try:
            current = self._mode_repo.get().mode
        except AppModeNotSetError:
            # Safe-by-default: an unset mode behaves as PROPOSE everywhere else.
            current = Mode.PROPOSE
        with Vertical(id="settings-dialog"):
            yield Static("[bold]Auto-approve per server[/bold]")
            yield Static(f"Global mode: {mode_markup(current)}")
            yield Static("[dim]Space = toggle — Enter = save — Esc = cancel[/dim]")
            yield ListView(id="settings-list")
            yield Static("", id="settings-error")

    def on_mount(self) -> None:
        """Populate the list with every connection and its effective flag."""
        list_view = self.query_one("#settings-list", ListView)
        try:
            connections = self._connections.list_all()
            for connection in connections:
                allowed = self._settings.get_or_default(connection.alias).auto_allowed
                self._state[connection.alias] = allowed
                self._initial[connection.alias] = allowed
                _ = list_view.append(ServerRow(connection, auto_allowed=allowed))
        except sqlite3.Error as exc:
            error = CGATE_THEME.error
            _ = self.query_one("#settings-error", Static).update(
                f"[{error}]Could not read the servers:[/{error}] {exc}"
            )
            return
        if self._state:
            list_view.index = 0
        list_view.focus()

    def action_toggle_row(self) -> None:
        """Flip the highlighted row's in-memory flag (no writes until Enter)."""
        list_view = self.query_one("#settings-list", ListView)
        row = list_view.highlighted_child
        if not isinstance(row, ServerRow):
            return
        new_value = not self._state[row.alias]
        self._state[row.alias] = new_value
        row.set_auto_allowed(auto_allowed=new_value)

    def action_commit(self) -> None:
        """Persist every alias whose flag changed since the modal opened."""
        try:
            for alias, allowed in self._state.items():
                if allowed != self._initial[alias]:
                    _ = self._settings.set(
                        alias=alias,
                        auto_allowed=allowed,
                        updated_by=_updated_by(),
                    )
        except sqlite3.Error as exc:
            error = CGATE_THEME.error
            _ = self.query_one("#settings-error", Static).update(
                f"[{error}]Could not save the changes:[/{error}] {exc}"
            )
            return
        self.dismiss(True)  # noqa: FBT003 - ModalScreen[bool].dismiss takes the result positionally

    def action_cancel(self) -> None:
        """Dismiss without writing anything."""
        self.dismiss(False)  # noqa: FBT003 - ModalScreen[bool].dismiss takes the result positionally


__all__ = ["ServerSettingsModal"]

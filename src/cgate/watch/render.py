"""Rich rendering for the cgate watch TUI (spec §Colores en cgate watch)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.box import ROUNDED
from rich.panel import Panel

from cgate.db.types import Batch, Command, CommandStatus, ServerType

if TYPE_CHECKING:
    from rich.console import RenderableType


def render_lot_header(batch: Batch) -> str:
    """Render a teal title and an optional grey italic description."""
    lines = [f"[teal]{batch.title}[/teal]"]
    if batch.description:
        lines.append(f"[grey50 italic]{batch.description}[/grey50 italic]")
    return "\n".join(lines)


def render_waiting_notice(count: int) -> str:
    """Render the queued-lot notice in yellow with its triangle symbol."""
    return (
        f"[yellow]▲ {count} lote(s) nuevo(s) en espera — "
        "se muestran al terminar el actual[/yellow]"
    )


def server_badge(server_type: ServerType) -> str:
    """Render a compact, colored server-type badge."""
    match server_type:
        case ServerType.WINDOWS:
            return "[cyan]WIN[/cyan]"
        case ServerType.LINUX:
            return "[green]LNX[/green]"


def render_command_row(command: Command) -> str:
    """Render one command with a color-paired status symbol."""
    badge = server_badge(command.server_type)
    target = f"{badge} [dim]{command.server_alias}[/dim]"
    match command.status:
        case CommandStatus.PENDING:
            return f"[yellow]●[/yellow] {command.command}  {target}"
        case CommandStatus.APPROVED:
            return (
                f"[yellow]●[/yellow] [italic]{command.command}[/italic] "
                f"{target} [dim](approved)[/dim]"
            )
        case CommandStatus.EXECUTED:
            return f"[green]✓[/green] {command.command}  {target}"
        case CommandStatus.REJECTED:
            return f"[red]✗[/red] {command.command}  {target}"
        case CommandStatus.FAILED:
            return (
                f"[red]✗[/red] [italic]{command.command}[/italic] "
                f"{target} [dim](failed)[/dim]"
            )


def build_lot_panel(
    batch: Batch,
    commands_for_batch: list[Command],
) -> RenderableType:
    """Compose a lot header and its FIFO command rows in one panel."""
    body = [render_lot_header(batch), ""]
    body.extend(render_command_row(command) for command in commands_for_batch)
    return Panel("\n".join(body), box=ROUNDED, border_style="teal")

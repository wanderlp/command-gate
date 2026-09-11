"""Pure markup helpers for the cgate watch TUI (spec §Colores en cgate watch)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cgate.db.types import CommandStatus, ServerType

if TYPE_CHECKING:
    from cgate.db.types import Batch, Command

_RESULT_SNIPPET_MAX_CHARS: Final = 200

_STATUS_GLYPHS: Final[dict[CommandStatus, str]] = {
    CommandStatus.PENDING: "[yellow]●[/yellow]",
    CommandStatus.APPROVED: "[yellow]◐[/yellow]",
    CommandStatus.EXECUTED: "[green]✓[/green]",
    CommandStatus.REJECTED: "[red]✗[/red]",
    CommandStatus.FAILED: "[red]✗[/red]",
}


def status_glyph(status: CommandStatus) -> str:
    """Return the colored status symbol for one command."""
    return _STATUS_GLYPHS[status]


def server_badge(server_type: ServerType) -> str:
    """Render a compact, colored server-type badge."""
    match server_type:
        case ServerType.WINDOWS:
            return "[cyan]WIN[/cyan]"
        case ServerType.LINUX:
            return "[green]LNX[/green]"


def format_batch_header(batch: Batch) -> str:
    """Render a bold cyan title with an optional grey italic description below it."""
    lines = [f"[bold cyan]{batch.title}[/bold cyan]"]
    if batch.description:
        lines.append(f"[grey50 italic]{batch.description}[/grey50 italic]")
    return "\n".join(lines)


def format_waiting_notice(waiting_count: int) -> str:
    """Render the queued-lot notice, or an empty string when nothing is waiting."""
    if waiting_count <= 0:
        return ""
    return f"[yellow]▲ {waiting_count} lote(s) en espera[/yellow]"


def _result_snippet(result: str) -> str:
    """Trim a result blob to its first line, capped to a display-friendly length."""
    stripped = result.strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0]
    if len(first_line) > _RESULT_SNIPPET_MAX_CHARS:
        return first_line[:_RESULT_SNIPPET_MAX_CHARS] + "…"
    return first_line


def format_command_line(command: Command) -> str:
    """Render one command row: status glyph, text, server badge, and result snippet."""
    glyph = status_glyph(command.status)
    badge = server_badge(command.server_type)
    line = f"{glyph} {command.command}  {badge} [dim]{command.server_alias}[/dim]"
    if command.status is CommandStatus.EXECUTED and command.result:
        line += f"\n    [dim]{_result_snippet(command.result)}[/dim]"
    elif command.status is CommandStatus.FAILED and command.result:
        line += f"\n    [red dim]{_result_snippet(command.result)}[/red dim]"
    return line

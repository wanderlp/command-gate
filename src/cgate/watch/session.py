"""Main synchronous interactive loop for `cgate watch`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console

from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.watch.approval import (
    approve_one,
    approve_remaining,
    reject_one,
    reject_remaining,
)
from cgate.watch.queue import (
    active_batch,
    count_waiting,
    is_batch_resolved,
    pending_commands_in_batch,
)
from cgate.watch.render import build_lot_panel, render_command_row, render_waiting_notice

if TYPE_CHECKING:
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent

    from cgate.db.connection import Database
    from cgate.db.types import Batch, Command
    from cgate.executor.base import ExecutionResult

Choice = Literal["y", "n", "a", "r"]


@dataclass(frozen=True, slots=True)
class WatchContext:
    """Runtime collaborators shared by watcher decision handlers."""

    db: Database
    console: Console
    batches: BatchesRepo
    commands: CommandsRepo
    connections: ConnectionsRepo


def _build_keybindings() -> KeyBindings:
    """Build y/n/a/r bindings that submit immediately on keypress."""
    bindings = KeyBindings()

    def submit(event: KeyPressEvent, choice: Choice) -> None:
        event.current_buffer.text = choice
        event.current_buffer.validate_and_handle()

    @bindings.add("y")
    def approve(event: KeyPressEvent) -> None:
        submit(event, "y")

    @bindings.add("n")
    def reject(event: KeyPressEvent) -> None:
        submit(event, "n")

    @bindings.add("a")
    def approve_all(event: KeyPressEvent) -> None:
        submit(event, "a")

    @bindings.add("r")
    def reject_all(event: KeyPressEvent) -> None:
        submit(event, "r")

    _ = (approve, reject, approve_all, reject_all)
    return bindings


def _ask_choice(console: Console, prompt_session: PromptSession[str]) -> Choice:
    """Prompt until one valid single-key decision is received."""
    while True:
        try:
            text = prompt_session.prompt(HTML("<ansiblue>Approve? [y/n/a/r]</ansiblue> "))
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Interrupted by user. Exiting.[/yellow]")
            raise SystemExit(130) from None
        match text:
            case "y" | "n" | "a" | "r":
                return text
            case _:
                console.print(
                    f"[red]invalid choice '{text}' — expected y/n/a/r[/red]"
                )


def _render_execution(console: Console, result: ExecutionResult) -> None:
    """Render a compact color-and-symbol execution outcome."""
    if result.ok:
        console.print(
            f"  [green]✓ exited {result.exit_code}[/green] in {result.duration_ms}ms"
        )
        if result.stdout:
            console.print(f"  [dim]{result.stdout.strip()[:200]}[/dim]")
        return
    console.print(
        f"  [red]✗ failed ({result.error_kind}) in {result.duration_ms}ms[/red]"
    )
    if result.stdout:
        console.print(f"  [dim]{result.stdout.strip()[:200]}[/dim]")
    if result.stderr:
        console.print(f"  [dim]{result.stderr.strip()[:200]}[/dim]")


def _apply_choice(
    context: WatchContext,
    pending: list[Command],
    choice: Choice,
) -> None:
    """Apply one interactive choice to the current pending command slice."""
    command = pending[0]
    match choice:
        case "y":
            updated, result = approve_one(
                db=context.db,
                commands=context.commands,
                connections=context.connections,
                batches=context.batches,
                command_id=command.id,
            )
            if updated is not None:
                context.console.print(render_command_row(updated))
            if result is not None:
                _render_execution(context.console, result)
        case "n":
            updated = reject_one(
                commands=context.commands,
                batches=context.batches,
                command_id=command.id,
            )
            context.console.print(render_command_row(updated))
        case "a":
            results = approve_remaining(
                db=context.db,
                commands=context.commands,
                connections=context.connections,
                batches=context.batches,
                remaining=pending,
            )
            for updated, result in results:
                context.console.print(render_command_row(updated))
                if result is not None:
                    _render_execution(context.console, result)
        case "r":
            rejected = reject_remaining(
                commands=context.commands,
                batches=context.batches,
                remaining=pending,
            )
            for updated in rejected:
                context.console.print(render_command_row(updated))


def _process_lot(context: WatchContext, lot: Batch) -> None:
    """Prompt over one lot while refreshing its commands before every decision."""
    commands_in_lot = context.commands.list_for_batch(lot.id)
    context.console.print(build_lot_panel(lot, commands_in_lot))
    prompt_session: PromptSession[str] | None = None
    shown_waiting = -1

    while True:
        commands_in_lot = context.commands.list_for_batch(lot.id)
        pending = pending_commands_in_batch(commands_in_lot)
        waiting = count_waiting(context.batches)
        if waiting > 0 and waiting != shown_waiting:
            context.console.print(render_waiting_notice(waiting))
            shown_waiting = waiting
        if not pending:
            if is_batch_resolved(commands_in_lot):
                context.batches.mark_resolved(lot.id)
            return
        if prompt_session is None:
            prompt_session = PromptSession(key_bindings=_build_keybindings())
        _apply_choice(context, pending, _ask_choice(context.console, prompt_session))


def run_watch_session(db: Database) -> None:
    """Process pending batches and commands in FIFO order, then exit."""
    console = Console()
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    connections = ConnectionsRepo(db)
    context = WatchContext(db, console, batches, commands, connections)

    while True:
        lot = active_batch(batches)
        if lot is None:
            message = (
                "[dim]No pending batches. Polling for new ones is not implemented "
                "in Phase 1; re-run `cgate watch` when ready.[/dim]"
            )
            console.print(message)
            return
        _process_lot(context, lot)

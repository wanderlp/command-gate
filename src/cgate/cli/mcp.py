"""Manage cgate MCP server registration and stdio serving."""

from __future__ import annotations

import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from cgate.mcp_installer import (
    CONFIG_FILENAMES,
    current_binary_command,
    detect_clients,
    is_registered,
    register,
    unregister,
)

mcp_app = typer.Typer(
    help="Manage the MCP server: register with IA clients, run as server."
)
console = Console()


def _should_pause(no_pause: bool) -> bool:
    """Pause for human review unless --no-pause or stdin is not a TTY."""
    return not no_pause and sys.stdin.isatty()


def _wait_for_enter() -> None:
    """Block until the user presses Enter; silently no-op on EOF (pipe/CI)."""
    try:
        input("Press Enter to close...")
    except EOFError:
        pass


@mcp_app.command("install")
def install_cmd(
    *,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Skip Y/N confirmation per detected client."
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would be done without modifying any config files.",
        ),
    ] = False,
    no_pause: Annotated[
        bool,
        typer.Option(
            "--no-pause",
            help=(
                "Skip the 'Press Enter to close' wait at the end. "
                "Combine with --yes for a fully unattended install."
            ),
        ),
    ] = False,
) -> None:
    """Detect IA clients and register cgate's MCP server with each."""
    clients = detect_clients()
    if not clients:
        console.print("[yellow]No IA clients detected.[/yellow]")
        console.print("Expected config paths under your home:")
        for relative in CONFIG_FILENAMES.values():
            console.print(f"  - [dim]~/{relative}[/dim]")
        return

    console.print(f"Detected {len(clients)} IA client(s):")
    for client in clients:
        status = (
            "[green]registered[/green]"
            if is_registered(client)
            else "[red]not registered[/red]"
        )
        console.print(
            f"  - {client.label} [dim]({client.config_path})[/dim] -- {status}"
        )

    command, args = current_binary_command()
    console.print(
        f"\ncgate command to register: [bold]{command}[/bold] {' '.join(args)}"
    )

    if dry_run:
        console.print()
        console.print(Rule("[yellow]DRY RUN -- no changes will be made[/yellow]"))
        for client in clients:
            action = "Re-register" if is_registered(client) else "Register"
            console.print(f"  Would {action} {client.label} -> {client.config_path}")
        console.print(
            f"  With command: [bold]{command}[/bold] {' '.join(args)}"
        )
        return

    succeeded: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    for client in clients:
        registered = is_registered(client)
        prompt = (
            f"Re-register {client.label}? (updates existing entry)"
            if registered
            else f"Register {client.label}?"
        )
        if not yes and not typer.confirm(prompt, default=not registered):
            skipped.append(client.label)
            continue
        try:
            register(client, command, args)
        except OSError as exc:
            failed.append((client.label, str(exc)))
            console.print(f"[red]Failed to write {client.config_path}: {exc}[/red]")
            continue
        succeeded.append(client.label)
        console.print(
            f"[green]Registered {client.label}.[/green] "
            "Restart your client to load the new MCP server."
        )

    console.print()
    console.print(Rule("Summary"))
    console.print(f"  [green]Registered: {len(succeeded)}[/green]")
    if skipped:
        console.print(
            f"  [yellow]Skipped:    {len(skipped)}[/yellow]  ({', '.join(skipped)})"
        )
    if failed:
        console.print(
            f"  [red]Failed:     {len(failed)}[/red]   "
            f"({', '.join(label for label, _ in failed)})"
        )

    if _should_pause(no_pause):
        console.print()
        _wait_for_enter()

    if failed:
        raise typer.Exit(code=1)


@mcp_app.command("uninstall")
def uninstall_cmd(
    *,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip confirmation.")
    ] = False,
) -> None:
    """Remove cgate from all detected IA client configs."""
    clients = [client for client in detect_clients() if is_registered(client)]
    if not clients:
        console.print(
            "[dim]cgate is not registered with any detected IA client.[/dim]"
        )
        return
    write_failed = False
    for client in clients:
        if not yes and not typer.confirm(
            f"Unregister from {client.label}?", default=True
        ):
            continue
        try:
            _ = unregister(client)
        except OSError as exc:
            write_failed = True
            console.print(f"[red]Failed to write {client.config_path}: {exc}[/red]")
            continue
        console.print(f"[green]Unregistered {client.label}.[/green]")
    if write_failed:
        raise typer.Exit(code=1)


@mcp_app.command("status")
def status_cmd() -> None:
    """Show which detected IA clients have cgate registered."""
    clients = detect_clients()
    if not clients:
        console.print(
            "[dim]No IA clients detected (no Claude Code/opencode/Cursor config files found).[/dim]"
        )
        return
    table = Table(title="cgate MCP registration status")
    table.add_column("Client", style="bold")
    table.add_column("Config path")
    table.add_column("Status")
    for client in clients:
        status = (
            "[green][OK] registered[/green]"
            if is_registered(client)
            else "[dim]not registered[/dim]"
        )
        table.add_row(client.label, str(client.config_path), status)
    console.print(table)


@mcp_app.command("serve")
def serve_cmd() -> None:
    """Run the MCP server on standard input and output for IA clients."""
    from cgate.mcp_server.server import main as serve_main  # noqa: PLC0415

    serve_main()

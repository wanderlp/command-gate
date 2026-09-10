"""Remove cgate from this machine: binary, data, MCP registrations, or all."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cgate.connections.auth import remove_credential
from cgate.connections.store import ConnectionsRepo
from cgate.core.paths import data_dir, db_path
from cgate.db.connection import Database, init_database
from cgate.db.types import Connection
from cgate.mcp_installer import ClientInstall, detect_clients, is_registered, unregister
from cgate.update import current_binary_path

console = Console()


def uninstall_cmd(
    *,
    binary: Annotated[
        bool,
        typer.Option(
            "--binary",
            help="Only remove the cgate binary (skips data and MCP cleanup).",
        ),
    ] = False,
    data: Annotated[
        bool,
        typer.Option(
            "--data",
            help="Only remove the data directory (DB + OS keyring entries).",
        ),
    ] = False,
    mcp: Annotated[
        bool,
        typer.Option(
            "--mcp",
            help="Only unregister cgate from detected IA clients.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip every confirmation prompt.",
        ),
    ] = False,
) -> None:
    """Remove cgate from this machine.

    Without any scope flag, removes everything. Combine flags to limit
    scope (e.g. `--data --yes` for an unattended reset). Without --yes,
    each destructive step prompts individually so partial failures leave
    the rest of the install recoverable.
    """
    do_all = not (binary or data or mcp)
    targets = {
        "binary": binary or do_all,
        "data": data or do_all,
        "mcp": mcp or do_all,
    }

    binary_path = current_binary_path()
    data_path = data_dir()
    mcp_clients: list[ClientInstall] = (
        [c for c in detect_clients() if is_registered(c)] if targets["mcp"] else []
    )

    # Each target is only actionable if the user asked for it AND there is
    # something to act on. Without this distinction `cgate uninstall` in a
    # dev environment would silently print three "nothing here" lines
    # instead of one concise "Nothing to do".
    binary_actionable = (
        targets["binary"] and binary_path is not None and binary_path.exists()
    )
    data_actionable = targets["data"] and data_path.exists()
    mcp_actionable = targets["mcp"] and bool(mcp_clients)

    console.print("[bold]Will remove:[/bold]")
    if targets["mcp"]:
        if mcp_actionable:
            console.print(f"  MCP: unregister from {len(mcp_clients)} client(s):")
            for c in mcp_clients:
                console.print(f"    - {c.label}")
        else:
            console.print("  MCP: [dim](no clients currently registered)[/dim]")
    if targets["data"]:
        if data_actionable:
            console.print(f"  Data: [bold]{data_path}[/bold]")
        else:
            console.print(f"  Data: [dim]{data_path} (does not exist)[/dim]")
    if targets["binary"]:
        if binary_path and binary_path.exists():
            console.print(f"  Binary: [bold]{binary_path}[/bold]")
        elif binary_path:
            console.print(f"  Binary: [dim]{binary_path} (already gone)[/dim]")
        else:
            console.print(
                "  Binary: [dim]not running from a frozen PyInstaller binary[/dim]"
            )

    # Order: MCP first (external state, easy to redo), then data (local state),
    # then binary last (the running program itself). Each helper handles its
    # own "not actionable" state (no clients, missing dir, dev env) so the
    # output stays informative even when one or more scopes are no-ops.
    if targets["mcp"]:
        _uninstall_mcp(mcp_clients, yes)
    if targets["data"]:
        _uninstall_data(data_path, yes)
    if targets["binary"]:
        _uninstall_binary(binary_path, yes)

    # Only summarise as "Nothing to do" when the user did not request any
    # specific scope (do_all path) AND none of the scopes were actionable;
    # otherwise prefer "Uninstall complete" even when individual helpers
    # reported no-ops, since the user explicitly asked for that work.
    explicit_flags = binary or data or mcp
    if (
        not explicit_flags
        and not binary_actionable
        and not data_actionable
        and not mcp_actionable
    ):
        console.print("[yellow]Nothing to do.[/yellow]")
    else:
        console.print("[green]Uninstall complete.[/green]")


def _uninstall_mcp(clients: list[ClientInstall], yes: bool) -> None:
    if not clients:
        console.print("  [dim]No MCP clients to unregister.[/dim]")
        return
    for client in clients:
        if not yes and not typer.confirm(
            f"Unregister from {client.label}?", default=True
        ):
            console.print(f"  [dim]Skipped {client.label}.[/dim]")
            continue
        try:
            _ = unregister(client)
            console.print(f"  Unregistered [bold]{client.label}[/bold].")
        except OSError as exc:
            console.print(
                f"  [red]Failed to unregister {client.label}:[/red] {exc}"
            )


def _uninstall_data(data_path: Path, yes: bool) -> None:
    if not data_path.exists():
        console.print(f"  [dim]{data_path} already gone.[/dim]")
        return

    if not yes and not typer.confirm(
        f"Delete data directory {data_path}?", default=False
    ):
        console.print("  [dim]Skipped.[/dim]")
        return

    # Enumerate connections first so we can clean each connection's
    # OS keyring entry before the DB row that names it disappears.
    connections: list[Connection] = []
    try:
        db = Database(path=db_path())
        init_database(db)
        connections = ConnectionsRepo(db).list_all()
    except Exception as exc:  # noqa: BLE001 - listing must not block cleanup
        console.print(
            f"  [yellow]Could not enumerate connections:[/yellow] {exc}"
        )

    for conn in connections:
        try:
            remove_credential(conn.alias)
            console.print(f"  Removed keyring entry for [bold]{conn.alias}[/bold].")
        except Exception as exc:  # noqa: BLE001 - per-credential failures are non-fatal
            console.print(
                f"  [yellow]Could not remove keyring for {conn.alias}:[/yellow] {exc}"
            )

    try:
        shutil.rmtree(data_path)
        console.print(f"  Removed [bold]{data_path}[/bold].")
    except OSError as exc:
        console.print(f"  [red]Failed to remove {data_path}:[/red] {exc}")


def _uninstall_binary(binary_path: Path | None, yes: bool) -> None:
    if binary_path is None:
        console.print(
            "  [dim]Not running from a PyInstaller binary; nothing to remove.[/dim]"
        )
        return
    if not binary_path.exists():
        console.print(f"  [dim]{binary_path} already gone.[/dim]")
        return

    if not yes and not typer.confirm(
        f"Delete binary at {binary_path}?", default=False
    ):
        console.print("  [dim]Skipped.[/dim]")
        return

    try:
        binary_path.unlink()
        console.print(f"  Removed [bold]{binary_path}[/bold].")
    except PermissionError as exc:
        # Windows holds an open-file lock on the running executable, so a
        # in-process unlink fails with EACCES. The user must close cgate
        # first and re-run, or delete the file by hand.
        console.print(f"  [yellow]Could not delete binary:[/yellow] {exc}")
        console.print(
            "  [dim]Close any running cgate process and re-run "
            "`cgate uninstall --binary`, or delete the file manually.[/dim]"
        )
    except OSError as exc:
        console.print(f"  [red]Could not delete binary:[/red] {exc}")

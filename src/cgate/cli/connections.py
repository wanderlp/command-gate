"""CLI group for saved server connections."""

from __future__ import annotations

import sqlite3

import typer
from click import Choice
from rich.console import Console
from rich.table import Table

from cgate.connections.auth import (
    is_kerberos_available,
    remove_credential,
    store_credential,
)
from cgate.connections.detect import (
    AmbiguousHostError,
    UnknownHostError,
    probe_host,
)
from cgate.connections.store import ConnectionsRepo
from cgate.core.paths import db_path
from cgate.db.connection import Database, init_database
from cgate.db.types import ServerType

connections_app = typer.Typer(help="Manage saved server connections.")
console = Console()


def _db() -> Database:
    """Open the local SQLite database and create its schema when absent."""
    db = Database(path=db_path())
    init_database(db)
    return db


@connections_app.command("add")
def add(alias: str, hostname: str) -> None:
    """Detect the server type, save the connection, and collect credentials."""
    # Pre-check alias before doing any network probe or prompting for
    # credentials. Without this, a duplicate alias wastes the user's
    # time on a probe plus username/password prompts before the
    # IntegrityError surfaces. See issue #16.
    repo = ConnectionsRepo(_db())
    if repo.get(alias) is not None:
        console.print(
            f"[red]Error:[/red] A connection with alias [bold]'{alias}'[/bold] "
            f"already exists. Use [bold]cgate connections remove {alias}[/bold] "
            f"first, or choose a different alias."
        )
        raise typer.Exit(code=1) from None

    probe = probe_host(hostname)
    try:
        server_type = probe.server_type
    except AmbiguousHostError:
        console.print(
            f"[yellow]{hostname} responded to BOTH SSH and WinRM. Choose one.[/yellow]"
        )
        choice = typer.prompt("Protocol", type=Choice(["ssh", "winrm"]))
        server_type = (
            ServerType.LINUX if choice == "ssh" else ServerType.WINDOWS
        )
    except UnknownHostError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    try:
        _ = repo.add(
            alias=alias,
            hostname=hostname,
            server_type=server_type,
            detection_ssh=probe.ssh,
            detection_winrm=probe.winrm,
        )
    except sqlite3.IntegrityError:
        # Race fallback: between the pre-check above and this INSERT,
        # another process could have added the same alias. Surface the
        # same friendly message instead of a raw traceback (issue #1).
        console.print(
            f"[red]Error:[/red] A connection with alias [bold]'{alias}'[/bold] "
            f"already exists. Use [bold]cgate connections remove {alias}[/bold] "
            f"first, or choose a different alias."
        )
        raise typer.Exit(code=1) from None
    console.print(f"Saved [bold]{alias}[/bold] -> {hostname} ({server_type.value}).")

    if is_kerberos_available():
        console.print(
            "[dim]Kerberos passthrough detected; no credentials to store.[/dim]"
        )
        return

    username = typer.prompt("Username")
    if server_type is ServerType.LINUX:
        ssh_key = typer.prompt(
            "SSH key path (blank for password auth)", default="", show_default=False
        )
        if ssh_key.strip():
            store_credential(
                alias, username=username, password=None, ssh_key=ssh_key.strip()
            )
            console.print(
                f"Stored SSH-key credential for [bold]{alias}[/bold] in OS keyring."
            )
        else:
            password = typer.prompt("Password", hide_input=True)
            store_credential(
                alias, username=username, password=password, ssh_key=None
            )
            console.print(
                f"Stored password credential for [bold]{alias}[/bold] in OS keyring."
            )
    else:
        password = typer.prompt("Password", hide_input=True)
        store_credential(alias, username=username, password=password, ssh_key=None)
        console.print(
            f"Stored WinRM credential for [bold]{alias}[/bold] in OS keyring."
        )


@connections_app.command("list")
def list_cmd() -> None:
    """List all saved connections in a table."""
    conns = ConnectionsRepo(_db()).list_all()
    if not conns:
        console.print("[dim]No connections saved.[/dim]")
        return
    table = Table(title="Saved connections")
    table.add_column("Alias", style="bold")
    table.add_column("Hostname")
    table.add_column("Type")
    table.add_column("SSH?", justify="center")
    table.add_column("WinRM?", justify="center")
    table.add_column("Created")
    for connection in conns:
        table.add_row(
            connection.alias,
            connection.hostname,
            connection.server_type.value,
            "Y" if connection.detection_ssh else "N",
            "Y" if connection.detection_winrm else "N",
            connection.created_at.isoformat(),
        )
    console.print(table)


@connections_app.command("remove")
def remove(alias: str) -> None:
    """Remove a connection and its credential from the OS keyring."""
    if not typer.confirm(f"Remove connection '{alias}'?"):
        raise typer.Abort
    repo = ConnectionsRepo(_db())
    if repo.get(alias) is None:
        console.print(f"[red]No connection '{alias}' found.[/red]")
        raise typer.Exit(code=1)

    # Remove the keyring credential BEFORE the DB row (issue #5). If the
    # keyring backend is unavailable -- common on headless Linux, and not
    # every backend failure is a keyring.errors.KeyringError subclass --
    # aborting here leaves the connection intact and reusable instead of
    # deleting the DB row and orphaning a credential no connection can
    # ever reference again.
    try:
        _ = remove_credential(alias)
    except Exception as exc:
        console.print(
            f"[red]Could not remove the stored credential for '{alias}' from "
            f"the OS keyring:[/red] {exc}\n"
            "[dim]The connection was NOT removed. Fix the keyring backend and "
            "retry, or remove the credential manually first.[/dim]"
        )
        raise typer.Exit(code=1) from exc

    removed = repo.remove(alias)
    if not removed:
        console.print(f"[red]No connection '{alias}' found.[/red]")
        raise typer.Exit(code=1)
    console.print(f"Removed [bold]{alias}[/bold].")

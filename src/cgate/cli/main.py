"""Top-level command-gate CLI."""

from __future__ import annotations

import sqlite3
from typing import Annotated

import typer
from rich.console import Console

from cgate import __version__
from cgate.cli.connections import connections_app
from cgate.cli.mcp import mcp_app
from cgate.cli.uninstall import uninstall_cmd
from cgate.cli.update import update_app
from cgate.cli.watch import watch_app

app = typer.Typer(
    name="cgate",
    help="Middleware/CLI between AI agents and servers -- IA proposes, human approves.",
    no_args_is_help=True,
    invoke_without_command=True,
)
app.add_typer(connections_app, name="connections")
app.add_typer(mcp_app, name="mcp")
app.add_typer(update_app, name="update")
app.add_typer(watch_app, name="watch")
app.command("uninstall")(uninstall_cmd)


def _version_callback(*, value: bool) -> None:
    if value:
        typer.echo(f"cgate {__version__}")
        raise typer.Exit(code=0)


@app.callback()
def root(
    *,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Display help or route to a command group."""


def main() -> None:
    """Run the CLI, turning an uncaught sqlite3.Error into a clean exit.

    Every command opens its own SQLite connection with no shared boundary
    that catches DB failures; a locked database (another cgate process
    holding it) or a corrupted file otherwise surfaces as a raw traceback
    from deep inside whichever command hit it first, instead of a clear
    message (issue #12). This is the entry point PyInstaller's bundled
    binary and the pip console-script both call, so it covers every
    command without needing its own try/except.
    """
    try:
        app()
    except sqlite3.Error as exc:
        Console(stderr=True).print(
            f"[red]cgate's local database is locked or corrupted:[/red] {exc}\n"
            "[dim]Another cgate process may be holding it, or the file may "
            "be damaged.[/dim]"
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

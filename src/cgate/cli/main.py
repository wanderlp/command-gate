"""Top-level command-gate CLI."""

from __future__ import annotations

from typing import Annotated

import typer

from cgate import __version__
from cgate.cli.connections import connections_app
from cgate.cli.watch import watch_app

app = typer.Typer(
    name="cgate",
    help="Middleware/CLI between AI agents and servers — IA proposes, human approves.",
    no_args_is_help=True,
    invoke_without_command=True,
)
app.add_typer(connections_app, name="connections")
app.add_typer(watch_app, name="watch")


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


if __name__ == "__main__":
    app()

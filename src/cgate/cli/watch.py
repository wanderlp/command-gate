"""CLI group for the interactive approval queue (`cgate watch`)."""

from __future__ import annotations

import typer

from cgate.core.paths import db_path
from cgate.db.connection import Database, init_database
from cgate.watch import run_watch_session

watch_app = typer.Typer(help="Interactive approval queue for proposed commands.")


@watch_app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Run the watcher session when no future subcommand was selected."""
    if ctx.invoked_subcommand is None:
        db = Database(path=db_path())
        init_database(db)
        run_watch_session(db)

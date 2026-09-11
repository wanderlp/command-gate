"""Entry point for `cgate watch`: fast-exit when idle, else the live dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console

from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.watch.app import WatchApp

if TYPE_CHECKING:
    from cgate.db.connection import Database


def run_watch_session(db: Database) -> None:
    """Print an empty-queue notice and exit, or launch the live approval dashboard.

    The dashboard, once open, never exits on its own: it polls the database for
    new batches and idles when the queue drains, so it survives proposals that
    arrive while a human is mid-review. Starting with nothing to review at all
    is the one case that still exits immediately, so `cgate watch` stays usable
    in scripts and CI.
    """
    batches = BatchesRepo(db)
    if not batches.list_pending():
        message = (
            "[dim]No pending batches. This window won't reopen itself — "
            "re-run `cgate watch` once one exists.[/dim]"
        )
        Console().print(message)
        return
    commands = CommandsRepo(db)
    connections = ConnectionsRepo(db)
    WatchApp(db=db, batches=batches, commands=commands, connections=connections).run()

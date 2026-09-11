"""Entry point for `cgate watch`: always launches the live approval dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cgate.connections.store import ConnectionsRepo
from cgate.db.batches import BatchesRepo
from cgate.db.commands import CommandsRepo
from cgate.watch.app import WatchApp

if TYPE_CHECKING:
    from cgate.db.connection import Database


def run_watch_session(db: Database) -> None:
    """Launch the live approval dashboard, starting idle if the queue is empty.

    The dashboard never exits on its own: it polls the database for new
    batches and idles when the queue is empty or drains, so it survives
    proposals that arrive at any point while it's open, not only ones
    already pending when it was launched.
    """
    batches = BatchesRepo(db)
    commands = CommandsRepo(db)
    connections = ConnectionsRepo(db)
    WatchApp(db=db, batches=batches, commands=commands, connections=connections).run()

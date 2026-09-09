"""Persistence for saved connection aliases — parallels detect.py and auth.py."""
from __future__ import annotations

from datetime import UTC, datetime

from cgate.db.connection import Database, connect
from cgate.db.rows import iso, row_to_connection
from cgate.db.types import Connection, ServerType


class ConnectionsRepo:
    """Saved connection CRUD keyed by a user-defined alias."""

    _db: Database  # class-level annotation required by strict mode

    def __init__(self, db: Database) -> None:
        """Store the database handle for subsequent operations."""
        self._db = db

    def add(
        self,
        *,
        alias: str,
        hostname: str,
        server_type: ServerType,
        detection_ssh: bool,
        detection_winrm: bool,
    ) -> Connection:
        """Save a connection with its detection signals and current UTC timestamp."""
        now = datetime.now(UTC)
        with connect(self._db) as conn:
            _ = conn.execute(
                """
                INSERT INTO connections (alias, hostname, server_type,
                    detection_ssh, detection_winrm, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    alias,
                    hostname,
                    server_type.value,
                    detection_ssh,
                    detection_winrm,
                    iso(now),
                ),
            )
        return Connection(
            alias=alias,
            hostname=hostname,
            server_type=server_type,
            detection_ssh=detection_ssh,
            detection_winrm=detection_winrm,
            created_at=now,
        )

    def get(self, alias: str) -> Connection | None:
        """Fetch a connection by alias, or None if it is absent."""
        with connect(self._db) as conn:
            row = conn.execute(
                """
                SELECT alias, hostname, server_type, detection_ssh, detection_winrm,
                    created_at
                FROM connections WHERE alias = ?
                """,
                (alias,),
            ).fetchone()
        return row_to_connection(row) if row is not None else None

    def list_all(self) -> list[Connection]:
        """Return every saved connection ordered by alias."""
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT alias, hostname, server_type, detection_ssh, detection_winrm,
                    created_at
                FROM connections ORDER BY alias ASC
                """
            ).fetchall()
        return [row_to_connection(row) for row in rows]

    def remove(self, alias: str) -> bool:
        """Delete a connection and report whether a row was removed."""
        with connect(self._db) as conn:
            cursor = conn.execute(
                "DELETE FROM connections WHERE alias = ?",
                (alias,),
            )
        return cursor.rowcount > 0

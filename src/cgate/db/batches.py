"""BatchesRepo: CRUD + FIFO queue queries for the `batches` table."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from cgate.db.connection import Database, connect
from cgate.db.rows import iso, row_to_batch
from cgate.db.types import Batch, BatchId


class BatchesRepo:
    """Batch CRUD + queue queries (FIFO pending list, mark resolved)."""

    _db: Database  # class-level annotation required by strict mode

    def __init__(self, db: Database) -> None:
        """Store the database handle for subsequent operations."""
        self._db = db

    def create(
        self,
        *,
        title: str,
        description: str | None,
        requested_by_agent: str | None,
    ) -> Batch:
        """Create a new batch with a fresh UUID and the current UTC timestamp."""
        batch_id = BatchId(str(uuid4()))
        now = datetime.now(UTC)
        with connect(self._db) as conn:
            _ = conn.execute(
                """
                INSERT INTO batches
                    (id, title, description, requested_by_agent, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (batch_id, title, description, requested_by_agent, iso(now)),
            )
        return Batch(
            id=batch_id,
            title=title,
            description=description,
            requested_by_agent=requested_by_agent,
            created_at=now,
            resolved_at=None,
        )

    def get(self, batch_id: BatchId) -> Batch | None:
        """Fetch a batch by its ID, or None if not found."""
        with connect(self._db) as conn:
            row = conn.execute(
                """
                SELECT id, title, description, requested_by_agent, created_at, resolved_at
                FROM batches WHERE id = ?
                """,
                (batch_id,),
            ).fetchone()
        return row_to_batch(row) if row is not None else None

    def list_pending(self) -> list[Batch]:
        """Return pending batches (resolved_at IS NULL), oldest first (FIFO)."""
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT id, title, description, requested_by_agent, created_at, resolved_at
                FROM batches
                WHERE resolved_at IS NULL
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [row_to_batch(r) for r in rows]

    def mark_resolved(
        self, batch_id: BatchId, *, resolved_at: datetime | None = None
    ) -> None:
        """Stamp resolved_at; no-op if the batch is already resolved."""
        when = resolved_at if resolved_at is not None else datetime.now(UTC)
        with connect(self._db) as conn:
            _ = conn.execute(
                """
                UPDATE batches SET resolved_at = ?
                WHERE id = ? AND resolved_at IS NULL
                """,
                (iso(when), batch_id),
            )

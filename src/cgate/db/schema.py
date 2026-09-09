"""SQL schema for command-gate (Phase 1, SQLite).

Source of truth: spec-inicial.md, "Esquema de datos (SQLite)" section.
Designed to migrate to SQL Server in Phase 2 with the same columns.
"""
from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    requested_by_agent TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    position INTEGER NOT NULL,
    server_alias TEXT NOT NULL,
    server_type TEXT NOT NULL CHECK (server_type IN ('windows', 'linux')),
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'executed', 'failed')
    ),
    result TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (batch_id, position)
);

CREATE INDEX IF NOT EXISTS idx_commands_batch_id ON commands(batch_id);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
"""

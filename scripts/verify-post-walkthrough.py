r"""Post-walkthrough verification: read-only inspection of cgate state.

Use after completing the walkthrough (Steps 5-7) to confirm the full
propose -> approve -> execute -> audit flow worked end-to-end.

Run with:  uv run python scripts\verify-post-walkthrough.py
or:        .venv\Scripts\python.exe scripts\verify-post-walkthrough.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / "AppData" / "Local" / "command-gate" / "cgate.db"


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}")
        print("Run any cgate command first (e.g., `cgate connections list`) to init the schema.")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. Connections
    section("Saved connections")
    conns = list(conn.execute(
        "SELECT alias, hostname, server_type, detection_ssh, detection_winrm, created_at "
        "FROM connections ORDER BY alias"
    ))
    if not conns:
        print("  (none yet — Step 1 of the walkthrough not done)")
    else:
        print(f"  {len(conns)} connection(s):")
        for c in conns:
            print(
                f"  - {c['alias']:<20} {c['hostname']:<30} type={c['server_type']:<8} "
                f"ssh={'Y' if c['detection_ssh'] else 'N'} "
                f"winrm={'Y' if c['detection_winrm'] else 'N'}"
            )

    # 2. Commands by status
    section("Commands by status")
    rows = list(conn.execute(
        "SELECT status, COUNT(*) AS n FROM commands GROUP BY status ORDER BY status"
    ))
    if not rows:
        print("  (no commands yet — Steps 5-6 of the walkthrough not done)")
    else:
        for r in rows:
            print(f"  {r['status']:<12} {r['n']}")

    # 3. Most recent commands (audit trail)
    section("Most recent commands (audit trail)")
    recent = list(conn.execute(
        """
        SELECT c.id, c.server_alias, c.command, c.status,
               c.approved_by, c.created_at, c.resolved_at,
               substr(c.result, 1, 80) AS result_preview
        FROM commands c
        ORDER BY c.created_at DESC
        LIMIT 10
        """
    ))
    if not recent:
        print("  (empty)")
    else:
        for r in recent:
            resolved = r["resolved_at"] or "-"
            preview = (r["result_preview"] or "").replace("\n", " ")
            print(
                f"  [{r['status']:<10}] {r['server_alias']:<14} "
                f"approved_by={r['approved_by'] or '-':<14} "
                f"cmd={r['command'][:40]!r}"
            )
            print(f"      created={r['created_at']}  resolved={resolved}")
            if preview:
                print(f"      result: {preview}{'...' if len(r['result_preview'] or '') >= 80 else ''}")

    # 4. Batches (resolved_at IS NULL == pending; batches has no status column)
    section("Batches")
    batches = list(conn.execute(
        "SELECT id, title, requested_by_agent, created_at, resolved_at, "
        "       (resolved_at IS NOT NULL) AS resolved "
        "FROM batches ORDER BY created_at DESC LIMIT 10"
    ))
    if not batches:
        print("  (none yet)")
    else:
        for b in batches:
            status_label = "resolved" if b["resolved"] else "pending"
            print(
                f"  - [{status_label:<10}] {b['title'][:50]:<50} "
                f"by={b['requested_by_agent'] or '-':<10} "
                f"resolved_at={b['resolved_at'] or '(pending)'}"
            )

    # 5. Sanity checks
    section("Sanity checks")
    checks = []

    schema_v = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    checks.append(("schema_version >= 2", schema_v is not None and schema_v["version"] >= 2))

    has_conn_fk = list(conn.execute("PRAGMA foreign_key_list(commands)"))
    checks.append(("commands.batch_id FK -> batches.id", len(has_conn_fk) > 0))

    has_status_idx = list(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_commands_status'"
    ))
    checks.append(("commands.status index exists", len(has_status_idx) > 0))

    executed = conn.execute(
        "SELECT COUNT(*) AS n FROM commands WHERE status = 'executed'"
    ).fetchone()["n"]
    checks.append(("at least 1 executed command", executed > 0))

    for desc, ok in checks:
        mark = "OK" if ok else "WARN"
        print(f"  [{mark}] {desc}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# command-gate (`cgate`)

`command-gate` is middleware and a CLI between AI assistants and managed servers: AI
proposes commands, a human decides what runs, and the tool records what ran, where, why,
and with what result.

## Status

Phase 1 — skeleton

## Development

```console
uv sync
uv run cgate --version
uv run pytest
uv run ruff check .
```

## Layout

```text
src/cgate/
├── cli/          # Top-level CLI and command groups
├── connections/  # Saved server connections
├── core/         # Shared paths and primitives
├── db/           # Local SQLite persistence
├── executor/     # WinRM and SSH execution
├── mcp_server/   # Local MCP server
└── watch/        # Interactive approval queue
```

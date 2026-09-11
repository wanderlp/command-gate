# command-gate (`cgate`)

`command-gate` is middleware and a CLI between AI assistants and managed servers: AI
proposes commands, a human decides what runs, and the tool records what ran, where, why,
and with what result.

## Install

`cgate` ships as a single binary per platform. Pick yours.

### Windows

1. Download `cgate-windows-amd64.exe` from the [latest release](https://github.com/wanderlp/command-gate/releases/latest).
2. Move it somewhere on your `PATH` (e.g., `C:\Users\<you>\bin\`).
3. From any shell:

   ```powershell
   cgate mcp install
   ```

   This detects installed IA clients (Claude Code, opencode, Cursor) and registers
   `cgate` as an MCP server for each one you confirm.
4. Restart your IA client. Done — Claude Code now exposes `propose_command`,
   `list_connections`, and `check_status` as tools it can call.

### macOS / Linux

Same flow — download `cgate-macos-arm64` or `cgate-linux-x86_64` from the
[latest release](https://github.com/wanderlp/command-gate/releases/latest),
`chmod +x` it, move it to `~/.local/bin` or another `PATH` directory.

### Via package manager

Manifest templates live under [`packaging/`](./packaging/README.md). Publishing
them requires creating separate tap/bucket repos (Scoop convention:
`<user>/scoop-bucket`; Homebrew convention: `<user>/homebrew-tap`) and pasting
the manifests there with the placeholder SHA256 replaced. See
`packaging/README.md` for the full flow. Once published, users install with:

```powershell
# Scoop (Windows)
scoop bucket add wanderlp https://github.com/wanderlp/scoop-bucket
scoop install cgate
```

```bash
# Homebrew (macOS, Linux)
brew tap wanderlp/tap
brew install cgate
```

Winget is not yet supported — submit path requires PR to `microsoft/winget-pkgs`.

### First-run warnings (binaries are not code-signed)

- **Windows SmartScreen**: "Windows protected your PC" → click **More info** →
  **Run anyway**. One-time per machine.
- **macOS Gatekeeper**: "cgate can't be opened because it is from an unidentified
  developer" → right-click the binary → **Open** → confirm. One-time per machine.

Both are unsigned-binary friction, not bugs. Code signing is on the roadmap
(see Phase 2).

## Typical workflow

```text
# 1. IA agent calls propose_command via MCP.  Humans never see this step.

# 2. Run the watch dashboard: a full-screen queue + active-batch view.
$ cgate watch
# Sidebar shows the FIFO batch queue; the main panel shows the active
# batch's commands. y=approve  n=reject  a=approve rest  r=reject rest  q=quit.
# Stays open and keeps polling for new batches even when the queue drains.

# 3. Stay current.
$ cgate update check
Latest: cgate 0.2.0 (release v0.2.0)
Update available: 0.1.0 -> 0.2.0
Run `cgate update apply` to install.
```

## Status

Phase 1 complete: end-to-end propose → approve → execute → audit loop working,
plus a round of security/robustness hardening (host key verification, TLS
validation, DB race-condition fixes, and more).
183 unit/integration tests pass; ruff + basedpyright pass with a handful of
accepted pre-existing findings (no known bugs, just style/complexity debt).

## Development

```console
git clone https://github.com/wanderlp/command-gate.git
cd command-gate
uv sync --extra dev
uv run cgate --version
uv run pytest
uv run ruff check .
uv run basedpyright src
```

To build a release binary locally:

```console
uv run python scripts/build-binary.py
ls dist/
```

## Layout

```text
src/cgate/
├── cli/          # Top-level CLI: connections, mcp, update, watch
├── connections/  # Saved server connections + WinRM/SSH detection + keyring auth
├── core/         # Shared paths and primitives
├── db/           # Local SQLite persistence (batches, commands, connections)
├── executor/     # WinRM and SSH execution
├── mcp_server/   # MCP server exposing propose_command / list_connections / check_status
└── watch/        # Interactive approval queue (y/n/a/r controls)
```

Release assets are produced by `.github/workflows/release.yml` on every tag push
(`git tag v0.1.5 && git push origin main v0.1.5`) and uploaded as platform-native
binaries to a GitHub Release.

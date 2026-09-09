# Packaging manifests

Reference templates for distributing `cgate` via system package managers.

## Status

The manifests in this directory are **templates** — they reference a
placeholder SHA256 that must be replaced with the real value from a published
GitHub Release before they're usable.

After the first `cgate` release exists (the CI workflow at
`.github/workflows/release.yml` produces it on every tag push), replace
`PLACEHOLDER_UPDATE_AFTER_RELEASE` in each manifest with the actual hash
of the corresponding asset, then publish the manifest to the appropriate
repository (see below).

## Layout

- `scoop/cgate.template.json` — Scoop bucket manifest for Windows.
- `brew/cgate.template.rb` — Homebrew formula for macOS and Linux.

Winget is intentionally **not** included yet — winget requires a multi-file
manifest structure under `manifests/<first-letter>/<lowercase-name>/<version>/`
and a PR to `microsoft/winget-pkgs` (which has multi-week review). When you're
ready to go through that, mirror the same shape as these two templates.

## How to publish each

### Scoop (Windows)

Scoop buckets are git repositories. Convention is `<github-user>/scoop-bucket`.

1. Create a new repo `github.com/wanderlp/scoop-bucket`.
2. Copy `scoop/cgate.template.json` to `bucket/cgate.json`.
3. Replace `PLACEHOLDER_UPDATE_AFTER_RELEASE` with the SHA256 of
   `cgate-windows-amd64.exe` from the latest release
   (`sha256sum cgate-windows-amd64.exe`).
4. Push to `main`.

Users install with:

```powershell
scoop bucket add wanderlp https://github.com/wanderlp/scoop-bucket
scoop install cgate
```

### Homebrew (macOS, Linux)

Homebrew taps are git repositories. Convention is `<github-user>/homebrew-tap`.

1. Create a new repo `github.com/wanderlp/homebrew-tap`.
2. Copy `brew/cgate.template.rb` to `Formula/cgate.rb`.
3. Replace the `PLACEHOLDER_UPDATE_AFTER_RELEASE` line with the real SHA256
   of `cgate-macos-arm64`. The Intel variant on the `on_intel` block also
   needs its own URL and SHA256 if you publish a separate Intel binary.
4. Push to `main`.

Users install with:

```bash
brew tap wanderlp/tap
brew install cgate
```

## Maintenance

When `cgate` publishes a new release, repeat for each manifest:

1. Download the new binary asset.
2. Compute its SHA256.
3. Bump `version` and update `hash` (and `url` if the asset name changed).
4. Commit and push.

A future GitHub Action could automate this — point a workflow at the
release and have it open a PR against the scoop/tap repos. Skipped for
now to keep the surface small.

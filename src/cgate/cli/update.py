"""Discover and install newer cgate versions from GitHub Releases."""

from __future__ import annotations

import os
import shutil
import time
from typing import Annotated

import typer
from rich.console import Console

from cgate import __version__
from cgate.update import (
    Release,
    UpdateError,
    compare_versions,
    current_binary_path,
    download_to,
    fetch_latest_release,
    find_blocking_processes,
    kill_process,
    replace_binary,
    select_asset,
)

update_app = typer.Typer(help="Check for and apply updates from GitHub Releases.")
console = Console()

# Brief delay after killing a process so Windows releases the file lock
# before we retry the rename. Empirically 1s is enough on stock Windows 11;
# keep it short enough not to feel laggy in interactive use.
_KILL_SETTLE_SECONDS: float = 1.0


def _print_release_summary(release: Release) -> None:
    console.print(
        f"[bold]Latest:[/bold] cgate {release.version}  ([dim]{release.tag}[/dim])"
    )
    console.print(f"  {release.html_url}")


@update_app.command("check")
def check_cmd() -> None:
    """Show whether a newer version is available, without downloading."""
    try:
        release = fetch_latest_release()
    except UpdateError as exc:
        console.print(f"[red]Could not check for updates:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _print_release_summary(release)
    if compare_versions(__version__, release.version) >= 0:
        console.print(f"[green]cgate {__version__} is up to date.[/green]")
        return
    console.print(
        f"[yellow]Update available: {__version__} -> {release.version}[/yellow]"
    )
    console.print("Run [bold]cgate update apply[/bold] to install.")


@update_app.command("apply")
def apply_cmd(
    *,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help=(
                "Auto-kill running cgate processes that block the swap, "
                "without prompting. Required for fully unattended updates."
            ),
        ),
    ] = False,
) -> None:
    """Download, verify, and replace the running binary with the latest release."""
    try:
        release = fetch_latest_release()
    except UpdateError as exc:
        console.print(f"[red]Could not fetch updates:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if compare_versions(__version__, release.version) >= 0:
        console.print(f"[green]cgate {__version__} is already up to date.[/green]")
        return

    asset = select_asset(release)
    if asset is None:
        available = ", ".join(item.name for item in release.assets) or "(none)"
        message = "".join(
            (
                "[red]No binary for this platform in release ",
                f"{release.tag}. Available: {available}[/red]",
            )
        )
        console.print(message)
        raise typer.Exit(code=2)

    binary = current_binary_path()
    if binary is None:
        detail = "(not a PyInstaller binary).[/yellow]"
        message = f"[yellow]Cannot auto-install from a development environment {detail}"
        console.print(message)
        console.print(f"Download manually from: {release.html_url}")
        return

    staging = binary.with_name(binary.name + ".new")
    previous = binary.with_name(binary.name + ".previous")

    console.print(
        f"Downloading {asset.name} ({asset.size / 1024 / 1024:.1f} MB)..."
    )
    try:
        download_to(asset, staging)
    except UpdateError as exc:
        console.print(f"[red]Download failed:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    # Read the running binary to a `.previous` rollback slot before the swap
    # so a bad release can be reverted with a single rename. Copy (not move)
    # because the source may be locked for write but is readable on Windows.
    try:
        if previous.exists():
            previous.unlink()
        shutil.copyfile(binary, previous)
        rollback_msg = f"Rollback slot: [dim]{previous}[/dim]"
    except OSError as exc:
        rollback_msg = f"[yellow]Could not snapshot current binary:[/yellow] {exc}"

    err = replace_binary(staging, binary)
    if err is None:
        console.print(
            f"[green]Installed cgate {release.version}.[/green]\n"
            "[dim]Restart your IA client (Claude Code / opencode / Cursor) "
            "to load the new MCP server.[/dim]\n"
            f"{rollback_msg}"
        )
        return

    # Swap failed. Show diagnostics and offer kill+retry.
    console.print(f"[red]Could not replace the running binary:[/red] {err}")
    blockers = find_blocking_processes(binary, exclude_pid=os.getpid())
    if blockers:
        pid_list = ", ".join(str(pid) for pid in blockers)
        console.print(
            f"[yellow]Active cgate processes blocking the swap:[/yellow] "
            f"PID(s) {pid_list}"
        )
        proceed = force or typer.confirm(
            "Kill blocking processes and retry the swap?", default=False
        )
        if proceed:
            killed = [pid for pid in blockers if kill_process(pid)]
            if killed:
                console.print(
                    f"[green]Killed {len(killed)} process(es); "
                    f"waiting {_KILL_SETTLE_SECONDS:g}s for Windows to release locks...[/green]"
                )
                time.sleep(_KILL_SETTLE_SECONDS)
                err = replace_binary(staging, binary)
                if err is None:
                    console.print(
                        f"[green]Installed cgate {release.version}.[/green]\n"
                        "[dim]Restart your IA client to load the new MCP server.[/dim]\n"
                        f"{rollback_msg}"
                    )
                    return
                console.print(f"[red]Swap still failed after kill:[/red] {err}")

    console.print(
        "\n[yellow]Manual recovery:[/yellow]\n"
        f"  1. Stop every running 'cgate' process:\n"
        f"     [dim]taskkill /F /IM cgate.exe[/dim]\n"
        f"  2. Replace the binary:\n"
        f"     [dim]Move-Item -Force '{staging}' '{binary}'[/dim]\n"
        f"  3. Restart your IA client (Claude Code / opencode / Cursor)\n"
        f"\nStaged download: [bold]{staging}[/bold]\n"
        f"{rollback_msg}"
    )
    raise typer.Exit(code=4)

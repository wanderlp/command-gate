"""Discover and install newer cgate versions from GitHub Releases."""

from __future__ import annotations

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
    replace_binary,
    select_asset,
)

update_app = typer.Typer(help="Check for and apply updates from GitHub Releases.")
console = Console()


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
def apply_cmd() -> None:
    """Download and replace the running binary with the latest release."""
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
    try:
        console.print(
            f"Downloading {asset.name} ({asset.size / 1024 / 1024:.1f} MB)..."
        )
        download_to(asset, staging)
    except UpdateError as exc:
        console.print(f"[red]Download failed: {exc}[/red]")
        raise typer.Exit(code=3) from exc

    if replace_binary(staging, binary):
        console.print(
            f"[green]Installed cgate {release.version}.[/green] Restart your shell."
        )
        return
    message = "".join(
        (
            f"[yellow]Staged at {staging}.[/yellow] Could not replace the running binary ",
            "(Windows locks running executables). Stop any running 'cgate' and rename manually.",
        )
    )
    console.print(message)

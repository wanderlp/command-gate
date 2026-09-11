"""Discover and install newer cgate versions from GitHub Releases."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Annotated

import typer
from rich.console import Console

from cgate import __version__
from cgate.cli._swap_helper import append_log
from cgate.update import (
    Release,
    UpdateError,
    compare_versions,
    current_binary_path,
    download_to,
    fetch_latest_release,
    find_blocking_processes,
    find_mcp_serving_pids,
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
                "without prompting. Does NOT cover a process serving a live "
                "MCP session -- see --force-mcp. Required for fully "
                "unattended updates otherwise."
            ),
        ),
    ] = False,
    force_mcp: Annotated[
        bool,
        typer.Option(
            "--force-mcp",
            help=(
                "Also kill a blocking process that is serving a live MCP "
                "session for an IA client, without prompting. This "
                "immediately disconnects that client mid-session; only pass "
                "it when you know nothing is relying on the connection."
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

    # Swap failed. Branch based on WHO is blocking:
    # - Self (we hold the lock on our own executable): spawn helper updater
    # - Other cgate processes: kill+retry
    # - Nobody listed: swap failed for some other reason (permissions, AV)
    console.print(f"[red]Could not replace the running binary:[/red] {err}")
    blockers = find_blocking_processes(binary)
    self_pid = os.getpid()
    if self_pid in blockers:
        # No other blockers. The only thing holding the binary is us.
        # On Windows the running process locks its own executable, so an
        # in-process swap is impossible. Spawn cmd.exe to do the move
        # after this process exits (cmd.exe is not cgate.exe so it does
        # not hold the lock). The 4-second ping gives this process
        # time to fully release the file handle.
        append_log(
            f"update apply: direct swap failed ({err}), self-lock detected "
            f"(PID {self_pid}), spawning cmd.exe for {binary}"
        )
        if _spawn_delayed_swap(staging, binary):
            console.print(
                f"[green]Update staged.[/green] The move will complete "
                f"in the background after this process exits. Re-run "
                f"[bold]cgate --version[/bold] in a few seconds to confirm.\n"
                f"{rollback_msg}"
            )
            return
        console.print("[red]Could not spawn swap helper.[/red]")
    elif blockers:
        pid_list = ", ".join(str(pid) for pid in blockers)
        console.print(
            f"[yellow]Active cgate processes blocking the swap:[/yellow] "
            f"PID(s) {pid_list}"
        )
        mcp_pids = find_mcp_serving_pids(blockers)
        if mcp_pids:
            mcp_pid_list = ", ".join(str(pid) for pid in mcp_pids)
            console.print(
                f"[red]PID(s) {mcp_pid_list} appear to be serving a live MCP "
                "session for an IA client (Claude Code / opencode / Cursor). "
                "Killing it disconnects that session immediately: any "
                "in-flight tool call fails, and cgate cannot reconnect it "
                "for you -- you will need to restart the IA client "
                "afterward.[/red]"
            )
            proceed = force_mcp or typer.confirm(
                "Kill the live MCP session and retry the swap?", default=False
            )
        else:
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
        f"\nStaged download: [bold]{staging}[/bold]"
    )
    # The swap never happened on any path that reaches here, so `previous`
    # is just a redundant copy of the still-current, unreplaced binary --
    # not a real rollback slot. Clean it up rather than leaving it as
    # disk clutter (issue #15); `staging` stays, since the message above
    # points the user at it for the manual move.
    previous.unlink(missing_ok=True)
    raise typer.Exit(code=4)


def _spawn_delayed_swap(staging, target) -> bool:
    """Spawn a detached subprocess that moves ``staging`` to ``target``
    after the caller has had time to exit.

    Windows: ``cmd.exe /c "ping ... && move"`` because cmd.exe is not
    cgate.exe and therefore does not hold the file lock. The ``ping``
    burns ~4 seconds to let the caller fully release its handle on
    ``target``.

    POSIX: ``mv -f`` via start_new_session, no delay needed since there
    is no self-lock.
    """
    try:
        if sys.platform == "win32":
            # cmd.exe is the cleanest available process to do a move on
            # Windows. ``ping`` with -n 5 sends 4 pings (about 3-4s) and
            # exits 0; ``&`` chains commands. The quotes around paths
            # matter because Windows paths with spaces would otherwise
            # be split.
            cmd_str = (
                f'ping -n 5 127.0.0.1 > nul & '
                f'move /Y "{staging}" "{target}"'
            )
            subprocess.Popen(
                f"cmd.exe /c \"{cmd_str}\"",
                # DETACHED_PROCESS | CREATE_NO_WINDOW: detach from our console
                # AND suppress the new console Windows would otherwise open
                # for cmd.exe/ping.exe. DETACHED_PROCESS alone still flashes
                # a visible window since it only stops console inheritance,
                # not allocation of a fresh one.
                creationflags=0x00000008 | 0x08000000,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                ["mv", "-f", str(staging), str(target)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        append_log(f"update apply: failed to spawn delayed swap: {exc}")
        return False
    return True

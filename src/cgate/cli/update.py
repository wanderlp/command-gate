"""Discover and install newer cgate versions from GitHub Releases."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Annotated, Final

import typer
from rich.console import Console

from cgate import __version__
from cgate.core.paths import data_dir
from cgate.core.update_log import append_log
from cgate.update import (
    Release,
    UpdateError,
    compare_versions,
    current_binary_path,
    download_to,
    ensure_helper_binary,
    fetch_latest_release,
    find_blocking_processes,
    find_mcp_serving_pids,
    kill_process,
    replace_binary,
    select_asset,
    spawn_helper,
    verify_attestation,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cgate.update import Asset

update_app = typer.Typer(help="Check for and apply updates from GitHub Releases.")
console = Console()

# Brief delay after killing a process so Windows releases the file lock
# before we retry the rename. Empirically 1s is enough on stock Windows 11;
# keep it short enough not to feel laggy in interactive use.
_KILL_SETTLE_SECONDS: float = 1.0
# How many trailing update.log lines `cgate update status` shows -- enough
# to see the outcome of the last background op without dumping the whole
# file's history.
_STATUS_LOG_LINES: Final = 15


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


@update_app.command("status")
def status_cmd() -> None:
    """Show the current version and the outcome of the last background update/delete.

    A deferred swap/delete (see ``_swap_via_helper_or_fallback`` /
    ``cli.uninstall._delete_via_helper_or_fallback``) finishes after this
    process has already exited, so there was previously no way to check
    whether it actually succeeded short of re-running ``cgate --version``
    and eyeballing the number. This surfaces the existing signals --
    staged files and ``update.log`` -- rather than tracking job state.
    """
    console.print(f"cgate {__version__}")

    binary = current_binary_path()
    if binary is not None:
        staging = binary.with_name(binary.name + ".new")
        previous = binary.with_name(binary.name + ".previous")
        if staging.exists():
            console.print(
                f"[yellow]Staged download present:[/yellow] {staging}\n"
                "[dim]An update may still be finishing in the background, "
                "or a prior one didn't complete.[/dim]"
            )
        if previous.exists():
            console.print(f"[dim]Rollback snapshot present:[/dim] {previous}")

    log_path = data_dir() / "update.log"
    if not log_path.exists():
        console.print("[dim]No update.log yet -- no background update/uninstall has run.[/dim]")
        return

    lines = log_path.read_text(encoding="utf-8").splitlines()
    tail = lines[-_STATUS_LOG_LINES:]
    console.print(f"\n[bold]Last {len(tail)} line(s) of {log_path}:[/bold]")
    for line in tail:
        console.print(f"  {line}")


def _download_and_verify(asset: Asset, release: Release, staging: Path) -> None:
    """Download the release asset and verify its build-provenance attestation.

    Split out of ``apply_cmd`` to keep its branch/statement count from
    growing further -- the attestation check (issue #4) adds a second
    verification step on top of the existing digest check in
    ``download_to``, same reasoning that pulled out
    ``_attempt_swap_with_recovery`` during the reopen-issues pass. Raises
    ``typer.Exit(code=3)`` on either failure; a failed attestation also
    wipes ``staging`` so a rejected binary isn't left on disk.
    """
    console.print(f"Downloading {asset.name} ({asset.size / 1024 / 1024:.1f} MB)...")
    try:
        download_to(asset, staging)
    except UpdateError as exc:
        console.print(f"[red]Download failed:[/red] {exc}")
        raise typer.Exit(code=3) from exc

    console.print("Verifying build provenance attestation...")
    try:
        verify_attestation(asset, release)
    except UpdateError as exc:
        console.print(f"[red]Attestation verification failed:[/red] {exc}")
        console.print(
            "[dim]Refusing to install a binary that cannot be verified as "
            f"coming from our release workflow. See {release.html_url} to "
            "inspect the release manually.[/dim]"
        )
        _safe_unlink(staging)
        raise typer.Exit(code=3) from exc


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
    binary = current_binary_path()
    if binary is None:
        # Running from source: there's no staged swap to do, but the
        # download/install instructions still apply.
        try:
            release = fetch_latest_release()
        except UpdateError as exc:
            console.print(f"[red]Could not fetch updates:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        asset = select_asset(release)
        detail = "(not a PyInstaller binary).[/yellow]"
        message = f"[yellow]Cannot auto-install from a development environment {detail}"
        console.print(message)
        console.print(f"Download manually from: {release.html_url}")
        return

    # From here on, `binary`, `staging`, `previous` are well-defined.
    staging = binary.with_name(binary.name + ".new")
    previous = binary.with_name(binary.name + ".previous")
    # Surface leftover staged files BEFORE any early-exit so the user sees
    # them even when already up-to-date or the version check decides not
    # to proceed (issue #15).
    _warn_about_orphans(staging, previous)

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

    _download_and_verify(asset, release, staging)

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

    swap_handled_locally = False
    try:
        err = replace_binary(staging, binary)
        if err is None:
            console.print(
                f"[green]Installed cgate {release.version}.[/green]\n"
                "[dim]Restart your IA client (Claude Code / opencode / Cursor) "
                "to load the new MCP server.[/dim]\n"
                f"{rollback_msg}"
            )
            swap_handled_locally = True
            return

        _attempt_swap_with_recovery(
            staging=staging,
            binary=binary,
            rollback_msg=rollback_msg,
            release=release,
            force=force,
            force_mcp=force_mcp,
        )
        # If the helper returns without raising, the swap succeeded.
        swap_handled_locally = True
    except typer.Exit:
        # Manual recovery footer (code 4) cleaned `.previous` itself and
        # intentionally keeps `.new` for the user to move. Nothing to do.
        swap_handled_locally = True
        raise
    except BaseException:
        # Truly unexpected: between staging being written and a clean
        # handled exit, state is ambiguous. Wipe both staged files so we
        # don't leak orphans (issue #15). The exception propagates so the
        # user still sees the underlying error.
        if not swap_handled_locally:
            _safe_unlink(staging)
            _safe_unlink(previous)
        raise


def _attempt_swap_with_recovery(
    *,
    staging: Path,
    binary: Path,
    rollback_msg: str,
    release: Release,
    force: bool,
    force_mcp: bool,
) -> None:
    """On a failed direct swap, branch by blocker identity.

    Returns silently when a recovery path actually finished the install
    (delayed swap queued, or kill+retry succeeded). Otherwise prints the
    manual-recovery footer (which cleans ``staging.previous`` and points
    the user at ``staging``) and raises ``typer.Exit(code=4)``.

    Branch order:
    - Self serving MCP: loud warning + --force-mcp gate, then spawn helper.
    - Other cgate processes blocking: kill+retry, with their own MCP check.
    - Self only (Windows): straight delayed swap.
    - Nobody: genuine OS-level swap failure -> manual recovery.
    """
    release_version = release.version
    err = replace_binary(staging, binary)
    if err is None:
        console.print(
            f"[green]Installed cgate {release_version}.[/green]\n"
            "[dim]Restart your IA client (Claude Code / opencode / Cursor) "
            "to load the new MCP server.[/dim]\n"
            f"{rollback_msg}"
        )
        return

    console.print(f"[red]Could not replace the running binary:[/red] {err}")
    self_pid = os.getpid()
    # Exclude our own PID: on Windows tasklist always finds the running
    # executable under its own name, so without exclude_pid we'd always
    # take the self branch -- which previously hid the MCP-warning elif
    # for self-lock, the very scenario issue #19 calls out.
    blockers = find_blocking_processes(binary, exclude_pid=self_pid)
    # Check MCP across self + blockers once; reuse the split below.
    mcp_pids = find_mcp_serving_pids([self_pid, *blockers])
    self_is_mcp = self_pid in mcp_pids
    other_mcp = [pid for pid in mcp_pids if pid != self_pid]

    if self_is_mcp:
        # We are serving MCP for a live IA-client session. Killing this
        # process disconnects that session immediately -- same warning
        # the elif raises for other-process MCP servers (issue #19).
        console.print(
            f"[red]The running cgate process (PID {self_pid}) is currently "
            "serving a live MCP session for an IA client (Claude Code / "
            "opencode / Cursor). Killing it disconnects that session "
            "immediately: any in-flight tool call fails, and cgate cannot "
            "reconnect it for you -- you will need to restart the IA "
            "client afterward.[/red]"
        )
        proceed = force_mcp or typer.confirm(
            "Proceed with self-swap and disconnect the live MCP session?",
            default=False,
        )
        if proceed:
            if _swap_via_helper_or_fallback(
                staging, binary, wait_pids=[self_pid], release=release
            ):
                console.print(
                    "[green]Update staged.[/green] The move will complete "
                    "in the background after this process exits. Re-run "
                    f"[bold]cgate --version[/bold] in a few seconds to "
                    f"confirm.\n{rollback_msg}"
                )
                return
            console.print("[red]Could not spawn swap helper.[/red]")
        # declined or spawn failed -> fall through to manual recovery
    elif blockers:
        pid_list = ", ".join(str(pid) for pid in blockers)
        console.print(
            f"[yellow]Active cgate processes blocking the swap:[/yellow] "
            f"PID(s) {pid_list}"
        )
        if other_mcp:
            mcp_pid_list = ", ".join(str(pid) for pid in other_mcp)
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
                    f"waiting {_KILL_SETTLE_SECONDS:g}s for Windows to "
                    f"release locks...[/green]"
                )
                time.sleep(_KILL_SETTLE_SECONDS)
                err = replace_binary(staging, binary)
                if err is None:
                    console.print(
                        f"[green]Installed cgate {release_version}.[/green]\n"
                        "[dim]Restart your IA client to load the new MCP "
                        f"server.[/dim]\n{rollback_msg}"
                    )
                    return
                console.print(f"[red]Swap still failed after kill:[/red] {err}")
    elif sys.platform == "win32":
        # No other cgate processes and we aren't ourselves serving MCP.
        # On Windows the running executable is always locked by this
        # process, so the most likely cause is plain self-lock.
        if _swap_via_helper_or_fallback(
            staging, binary, wait_pids=[self_pid], release=release
        ):
            console.print(
                "[green]Update staged.[/green] The move will complete "
                "in the background after this process exits. Re-run "
                f"[bold]cgate --version[/bold] in a few seconds to confirm.\n"
                f"{rollback_msg}"
            )
            return
        console.print("[red]Could not spawn swap helper.[/red]")
    # else: non-Windows with no blockers -- genuine OS-level swap failure
    # (permissions, AV, etc.). Fall through to manual recovery.

    previous = binary.with_name(binary.name + ".previous")
    console.print(
        "\n[yellow]Manual recovery:[/yellow]\n"
        f"  [dim]We'll auto-apply this update the next time you run cgate "
        f"with no other cgate processes alive. Easiest way there:\n"
        f"   close every IA client connected to cgate (Claude Code / opencode / Cursor),\n"
        f"   or restart your computer. No action needed unless you want to apply now:[/dim]\n"
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


def _swap_via_helper_or_fallback(
    staging: Path, binary: Path, *, wait_pids: list[int], release: Release
) -> bool:
    """Prefer the compiled ``cgate-helper.exe`` to perform the swap.

    Falls back to the ``cmd.exe`` shell-chain when it isn't available or
    fails to spawn. The compiled helper does a real wait-for-exit on
    ``wait_pids`` (not the shell-chain's blind ~4s ``ping`` delay) and
    never shares an image name with ``cgate.exe``, so there is nothing to
    disambiguate on Windows's process list. ``release`` pairs the
    helper's version with the cgate version being installed
    (``ensure_helper_binary`` downloads and attestation-verifies it from
    that same release on first use).
    """
    helper = ensure_helper_binary(binary, release)
    if helper is not None:
        wait_args = [arg for pid in wait_pids for arg in ("--wait-pid", str(pid))]
        spawned = spawn_helper(
            helper, "replace", "--target", str(binary), "--source", str(staging), *wait_args
        )
        if spawned:
            append_log(f"update apply: dispatched compiled helper {helper} for {binary}")
            return True
        append_log("update apply: compiled helper spawn failed, falling back to shell-chain")
    return _spawn_delayed_swap(staging, binary)


def _spawn_delayed_swap(staging, target) -> bool:
    """Spawn a detached subprocess that moves ``staging`` to ``target``
    after the caller has had time to exit.

    Windows: ``cmd.exe /c "ping ... && move"`` because cmd.exe is not
    cgate.exe and therefore does not hold the file lock. The ``ping``
    burns ~4 seconds to let the caller fully release its handle on
    ``target``.

    POSIX: ``mv -f`` via start_new_session, no delay needed since there
    is no self-lock.

    The path arguments are routed through ``subprocess.list2cmdline`` so
    a path containing spaces, embedded double quotes, or backslash-quote
    sequences cannot break out of the argument (issue #44). The fixed
    ``ping``, the ``> nul`` redirection, the ``&`` chain operator, and
    the ``move /Y`` / ``del /F /Q`` invocations are literal -- they
    contain no user-controlled data and cannot smuggle a metacharacter.
    """
    try:
        if sys.platform == "win32":
            # cmd.exe is the cleanest available process to do a move on
            # Windows. ``ping`` with -n 5 sends 4 pings (about 3-4s) and
            # exits 0; ``&`` chains commands. The ``move`` invocation's
            # path arguments come through ``list2cmdline`` so spaces and
            # embedded quotes in a path do not break the argument.
            move_part = subprocess.list2cmdline(
                ["move", "/Y", str(staging), str(target)]
            )
            cmd_str = f"ping -n 5 127.0.0.1 > nul & {move_part}"
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


def _safe_unlink(path: Path) -> None:
    """Best-effort removal of a staged update file (issue #15).

    Swallows missing-file and permission/AV errors; the goal here is
    to avoid leaving orphans on disk, not to surface every failure.
    """
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _warn_about_orphans(staging: Path, previous: Path) -> None:
    """Surface leftover staged files from a previous run (issue #15).

    The download path silently overwrites ``staging`` and the snapshot
    step silently unlinks ``previous``, both of which hid the fact that
    a prior ``update apply`` exited without finishing. Warn the user so
    they can investigate; we still proceed (and overwrite) so a fresh
    attempt isn't blocked by stale leftovers.
    """
    if staging.exists():
        console.print(
            f"[yellow]Found leftover staged download from a previous run:[/yellow]\n"
            f"  [dim]{staging}[/dim] -- this run will overwrite it."
        )
    if previous.exists():
        console.print(
            f"[yellow]Found leftover rollback snapshot from a previous run:[/yellow]\n"
            f"  [dim]{previous}[/dim] -- this run will overwrite it."
        )

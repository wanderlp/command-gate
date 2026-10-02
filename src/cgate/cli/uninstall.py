"""Remove cgate from this machine: binary, data, MCP registrations, or all."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cgate.connections.auth import remove_credential
from cgate.connections.store import ConnectionsRepo
from cgate.core.paths import data_dir, db_path
from cgate.core.update_log import append_log
from cgate.db.connection import Database, init_database
from cgate.db.types import Connection
from cgate.mcp_installer import ClientInstall, detect_clients, is_registered, unregister
from cgate.update import (
    current_binary_path,
    ensure_helper_binary,
    find_blocking_processes,
    find_mcp_serving_pids,
    helper_binary_path,
    kill_process,
    spawn_helper,
)

# Brief delay after killing a blocking process so Windows releases the file
# lock before we retry the delete. Same value `update apply` uses for the
# analogous kill-and-retry swap (cli/update.py).
_KILL_SETTLE_SECONDS: float = 1.0

console = Console()


def uninstall_cmd(
    *,
    binary: Annotated[
        bool,
        typer.Option(
            "--binary",
            help="Only remove the cgate binary (skips data and MCP cleanup).",
        ),
    ] = False,
    data: Annotated[
        bool,
        typer.Option(
            "--data",
            help="Only remove the data directory (DB + OS keyring entries).",
        ),
    ] = False,
    mcp: Annotated[
        bool,
        typer.Option(
            "--mcp",
            help="Only unregister cgate from detected IA clients.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip every confirmation prompt.",
        ),
    ] = False,
) -> None:
    """Remove cgate from this machine.

    Without any scope flag, removes everything. Combine flags to limit
    scope (e.g. `--data --yes` for an unattended reset). Without --yes,
    each destructive step prompts individually so partial failures leave
    the rest of the install recoverable.
    """
    do_all = not (binary or data or mcp)
    targets = {
        "binary": binary or do_all,
        "data": data or do_all,
        "mcp": mcp or do_all,
    }

    binary_path = current_binary_path()
    data_path = data_dir()
    mcp_clients: list[ClientInstall] = (
        [c for c in detect_clients() if is_registered(c)] if targets["mcp"] else []
    )

    # Each target is only actionable if the user asked for it AND there is
    # something to act on. Without this distinction `cgate uninstall` in a
    # dev environment would silently print three "nothing here" lines
    # instead of one concise "Nothing to do".
    binary_actionable = (
        targets["binary"] and binary_path is not None and binary_path.exists()
    )
    data_actionable = targets["data"] and data_path.exists()
    mcp_actionable = targets["mcp"] and bool(mcp_clients)

    console.print("[bold]Will remove:[/bold]")
    if targets["mcp"]:
        if mcp_actionable:
            console.print(f"  MCP: unregister from {len(mcp_clients)} client(s):")
            for c in mcp_clients:
                console.print(f"    - {c.label}")
        else:
            console.print("  MCP: [dim](no clients currently registered)[/dim]")
    if targets["data"]:
        if data_actionable:
            console.print(f"  Data: [bold]{data_path}[/bold]")
        else:
            console.print(f"  Data: [dim]{data_path} (does not exist)[/dim]")
    if targets["binary"]:
        if binary_path and binary_path.exists():
            console.print(f"  Binary: [bold]{binary_path}[/bold]")
        elif binary_path:
            console.print(f"  Binary: [dim]{binary_path} (already gone)[/dim]")
        else:
            console.print(
                "  Binary: [dim]not running from a frozen PyInstaller binary[/dim]"
            )

    # Order: MCP first (external state, easy to redo), then data (local state),
    # then binary last (the running program itself). Each helper handles its
    # own "not actionable" state (no clients, missing dir, dev env) so the
    # output stays informative even when one or more scopes are no-ops.
    mcp_ok = True
    data_ok = True
    binary_status = "not_applicable"
    if targets["mcp"]:
        mcp_ok = _uninstall_mcp(mcp_clients, yes)
    if targets["data"]:
        data_ok = _uninstall_data(data_path, yes)
    if targets["binary"]:
        binary_status = _uninstall_binary(binary_path, yes)

    # Only summarise as "Nothing to do" when the user did not request any
    # specific scope (do_all path) AND none of the scopes were actionable.
    # Otherwise the summary must reflect what each helper actually
    # accomplished -- printing "Uninstall complete" regardless of per-step
    # outcome previously hid real failures (e.g. a locked binary on
    # Windows) behind a misleading green line.
    explicit_flags = binary or data or mcp
    if (
        not explicit_flags
        and not binary_actionable
        and not data_actionable
        and not mcp_actionable
    ):
        console.print("[yellow]Nothing to do.[/yellow]")
    elif binary_status == "deferred":
        console.print(
            "[green]Uninstall complete[/green] [dim](the binary is locked by "
            "this running process and will be deleted automatically a few "
            "seconds after it exits -- no further action needed).[/dim]"
        )
    elif not mcp_ok or not data_ok or binary_status in ("failed", "skipped"):
        console.print(
            "[yellow]Uninstall finished with unresolved steps -- see warnings above.[/yellow]"
        )
    else:
        console.print("[green]Uninstall complete.[/green]")


def _uninstall_mcp(clients: list[ClientInstall], yes: bool) -> bool:
    """Unregister cgate from each detected IA client.

    Returns False if any client was skipped or failed to unregister, so
    the final summary can report unresolved steps instead of a blanket
    "Uninstall complete.".
    """
    if not clients:
        console.print("  [dim]No MCP clients to unregister.[/dim]")
        return True
    all_ok = True
    for client in clients:
        if not yes and not typer.confirm(
            f"Unregister from {client.label}?", default=True
        ):
            console.print(f"  [dim]Skipped {client.label}.[/dim]")
            all_ok = False
            continue
        try:
            _ = unregister(client)
            console.print(f"  Unregistered [bold]{client.label}[/bold].")
        except OSError as exc:
            console.print(
                f"  [red]Failed to unregister {client.label}:[/red] {exc}"
            )
            all_ok = False
    return all_ok


def _uninstall_data(data_path: Path, yes: bool) -> bool:
    """Remove the data directory, including keyring entries for its connections.

    Returns False on skip or any failure, so the final summary can report
    unresolved steps instead of a blanket "Uninstall complete.".
    """
    if not data_path.exists():
        console.print(f"  [dim]{data_path} already gone.[/dim]")
        return True

    if not yes and not typer.confirm(
        f"Delete data directory {data_path}?", default=False
    ):
        console.print("  [dim]Skipped.[/dim]")
        return False

    ok = True

    # Enumerate connections first so we can clean each connection's
    # OS keyring entry before the DB row that names it disappears.
    # If enumeration fails (DB locked, file unreadable, ...), refuse to
    # proceed to rmtree -- doing so would destroy the only index that
    # names which keyring entries exist, orphaning the credentials for
    # every connection permanently with no recovery path (issue #40).
    connections: list[Connection] = []
    connection_enum_ok = True
    try:
        db = Database(path=db_path())
        init_database(db)
        connections = ConnectionsRepo(db).list_all()
    except Exception as exc:  # noqa: BLE001 - want every DB error surfaced here
        console.print(f"  [red]Could not enumerate connections:[/red] {exc}")
        console.print(
            "  [red]Refusing to delete the data directory -- doing so would "
            "orphan the keyring credentials for every connection.[/red]\n"
            "  [dim]Close any process locking cgate.db (the cgate watch "
            "TUI, another cgate invocation, the sqlite3 CLI, ...) and "
            "retry the uninstall.[/dim]"
        )
        connection_enum_ok = False
        ok = False

    if connection_enum_ok:
        for conn in connections:
            try:
                remove_credential(conn.alias)
                console.print(f"  Removed keyring entry for [bold]{conn.alias}[/bold].")
            except Exception as exc:  # noqa: BLE001 - per-credential failures are non-fatal
                console.print(
                    f"  [yellow]Could not remove keyring for {conn.alias}:[/yellow] {exc}"
                )
                ok = False

        try:
            shutil.rmtree(data_path)
            console.print(f"  Removed [bold]{data_path}[/bold].")
        except OSError as exc:
            console.print(f"  [red]Failed to remove {data_path}:[/red] {exc}")
            ok = False

    return ok


def _uninstall_binary(binary_path: Path | None, yes: bool) -> str:
    """Delete the cgate binary.

    Returns one of "removed", "deferred", "skipped", "already_gone",
    "not_applicable", or "failed" so the caller can print an honest final
    summary instead of a blanket "Uninstall complete" regardless of outcome.
    """
    if binary_path is None:
        console.print(
            "  [dim]Not running from a PyInstaller binary; nothing to remove.[/dim]"
        )
        return "not_applicable"
    if not binary_path.exists():
        console.print(f"  [dim]{binary_path} already gone.[/dim]")
        return "already_gone"

    if not yes and not typer.confirm(
        f"Delete binary at {binary_path}?", default=False
    ):
        console.print("  [dim]Skipped.[/dim]")
        return "skipped"

    try:
        binary_path.unlink()
        console.print(f"  Removed [bold]{binary_path}[/bold].")
        _cleanup_helper_binary(binary_path)
        return "removed"
    except OSError as exc:
        if sys.platform != "win32":
            console.print(f"  [red]Could not delete binary:[/red] {exc}")
            return "failed"

    # Windows only, direct unlink failed above. The running cgate.exe always
    # holds a lock on its own image file while executing, so this is the
    # expected case, not a genuine error -- the exact self-lock problem
    # `update apply` already solves in cli/update.py. Reuse its blocker
    # detection instead of guessing whether the lock is us or someone else.
    self_pid = os.getpid()
    other_blockers = find_blocking_processes(binary_path, exclude_pid=self_pid)

    if other_blockers:
        pid_list = ", ".join(str(pid) for pid in other_blockers)
        console.print(
            f"  [yellow]Other running cgate processes are blocking the "
            f"delete:[/yellow] PID(s) {pid_list}"
        )
        mcp_pids = find_mcp_serving_pids(other_blockers)
        if mcp_pids:
            console.print(
                "  [red]One of them appears to be serving a live MCP "
                "session for an IA client (Claude Code / opencode / "
                "Cursor). Killing it disconnects that session "
                "immediately.[/red]"
            )
        proceed = yes or typer.confirm(
            "  Kill blocking process(es) and retry?", default=False
        )
        if not proceed:
            console.print(
                "  [dim]Skipped. Close those processes and re-run "
                "`cgate uninstall --binary` to finish.[/dim]"
            )
            return "skipped"
        if any(kill_process(pid) for pid in other_blockers):
            time.sleep(_KILL_SETTLE_SECONDS)
        try:
            binary_path.unlink()
            console.print(f"  Removed [bold]{binary_path}[/bold].")
            _cleanup_helper_binary(binary_path)
            return "removed"
        except OSError:
            pass  # still locked (likely by us) -- fall through below

    if _delete_via_helper_or_fallback(binary_path, wait_pids=[self_pid]):
        console.print(
            f"  [yellow]{binary_path}[/yellow] is locked by this running "
            "process. It will be deleted automatically a few seconds "
            "after this command exits -- no further action needed."
        )
        _cleanup_helper_binary(binary_path)
        return "deferred"

    console.print(
        "  [red]Could not delete binary:[/red] locked by this running "
        "process, and the background delete helper failed to start."
    )
    console.print(f'  [dim]Close cgate and delete manually: del "{binary_path}"[/dim]')
    return "failed"


def _cleanup_helper_binary(binary_path: Path) -> None:
    """Best-effort removal of the sibling ``cgate-helper.exe``.

    Called once the main binary is gone or on its way out. Never raises
    and never affects the overall uninstall outcome -- this is
    disk-clutter cleanup (the helper is an implementation detail the
    user never installed by hand), not core functionality. If the helper
    is still in use -- most likely because it's the very process
    performing a deferred delete of ``binary_path`` right now -- fall
    back to the ``cmd.exe`` shell-chain instead of another compiled
    helper: ``cmd.exe`` isn't a PyInstaller ``--onefile`` binary, so it
    has no self-lock problem to solve for a second time.
    """
    helper = helper_binary_path(binary_path)
    if not helper.exists():
        return
    try:
        helper.unlink()
    except OSError:
        _spawn_delayed_delete(helper)


def _delete_via_helper_or_fallback(target: Path, *, wait_pids: list[int]) -> bool:
    """Prefer the compiled ``cgate-helper.exe`` to delete ``target``.

    Falls back to the ``cmd.exe`` shell-chain when it isn't available or
    fails to spawn. There's no in-flight ``Release`` here (unlike
    ``update apply``, this isn't installing anything), so
    ``ensure_helper_binary`` pairs the helper with the *currently
    installed* version's own release tag instead of "latest".
    """
    helper = ensure_helper_binary(target)
    if helper is not None:
        wait_args = [arg for pid in wait_pids for arg in ("--wait-pid", str(pid))]
        if spawn_helper(helper, "delete", "--target", str(target), *wait_args):
            append_log(f"uninstall --binary: dispatched compiled helper {helper} for {target}")
            return True
        append_log("uninstall --binary: compiled helper spawn failed, falling back to shell-chain")
    return _spawn_delayed_delete(target)


def _spawn_delayed_delete(target: Path) -> bool:
    """Spawn a detached helper that deletes ``target`` a few seconds after this process exits.

    Windows only -- mirrors ``_spawn_delayed_swap`` in cli/update.py, which
    solves the identical self-lock problem for ``update apply``. ``cmd.exe``
    is not ``cgate.exe`` so it never holds the lock our own process does;
    the ``ping`` burns ~4s so our handle on the file is guaranteed closed
    (process exited) by the time ``del`` runs.

    The path argument is routed through ``subprocess.list2cmdline`` so a
    path containing spaces cannot break out of the argument (issue #44).
    This does NOT protect against an embedded ``"``: cmd.exe parses its
    own command line with simple quote-toggle rules (unrelated to the
    CommandLineToArgvW convention ``list2cmdline`` targets), so a path
    with a balanced pair of embedded quotes can re-open cmd.exe's
    metacharacter interpretation and inject a command. ``"`` is not a
    legal character in a Windows path, so ``target`` can never
    legitimately contain one -- the guard below turns that invariant
    into an enforced precondition instead of an assumption.
    """
    if '"' in str(target):
        msg = (
            "target path contains a literal '\"', which is not a legal "
            "Windows path character and which list2cmdline's escaping "
            "cannot make safe against cmd.exe's own parser -- refusing to "
            "build the delete command"
        )
        raise ValueError(msg)
    try:
        del_part = subprocess.list2cmdline(["del", "/F", "/Q", str(target)])
        cmd_str = f"ping -n 5 127.0.0.1 > nul & {del_part}"
        subprocess.Popen(
            f'cmd.exe /c "{cmd_str}"',
            # DETACHED_PROCESS | CREATE_NO_WINDOW, same combination
            # cli/update.py uses and for the same reason: detach from our
            # console AND suppress the window Windows would otherwise
            # flash for cmd.exe/ping.exe.
            creationflags=0x00000008 | 0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True

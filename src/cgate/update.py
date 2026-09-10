"""Self-update via GitHub Releases API. Pure logic; the CLI layer wraps it."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from packaging.version import InvalidVersion, Version
from pathlib import Path
from typing import Final, NotRequired, TypedDict

DEFAULT_REPO: Final = "wanderlp/command-gate"
GITHUB_API: Final = "https://api.github.com"
# Body read has no separate timeout; this only caps the connect/handshake.
# A 29 MB download on a 1 Mbps link takes ~230s; rely on TCP keepalive for
# stalled-transfer detection rather than a per-call wall clock.
REQUEST_TIMEOUT: Final = 30
_DOWNLOAD_CHUNK: Final = 65536


class _AssetPayload(TypedDict):
    name: str
    browser_download_url: str
    size: NotRequired[int]
    digest: NotRequired[str]


class _ReleasePayload(TypedDict, total=False):
    tag_name: str
    html_url: str
    assets: list[_AssetPayload]


@dataclass(frozen=True, slots=True)
class Asset:
    """A downloadable asset attached to a GitHub Release."""

    name: str
    download_url: str
    size: int
    # sha256:<hex>; empty string when the source does not provide one.
    digest: str

    @property
    def suffix(self) -> str:
        """Return the platform suffix after the ``cgate-`` prefix."""
        return self.name.removeprefix("cgate-")


@dataclass(frozen=True, slots=True)
class Release:
    """A GitHub Release with its downloadable assets."""

    tag: str
    version: str
    html_url: str
    assets: tuple[Asset, ...]


class UpdateError(Exception):
    """Network, parse, or filesystem error during update."""


def fetch_latest_release(repo: str = DEFAULT_REPO) -> Release:
    """Fetch the latest published GitHub Release via the public API."""
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    request = urllib.request.Request(  # noqa: S310 -- URL is fixed to the HTTPS GitHub API.
        url, headers={"Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- Request contains an HTTPS URL.
            request, timeout=REQUEST_TIMEOUT
        ) as response:
            payload = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        msg = f"failed to fetch {url}: {exc}"
        raise UpdateError(msg) from exc

    try:
        data: _ReleasePayload = json.loads(payload)
        return _parse_release(data)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        msg = f"malformed release JSON: {exc}"
        raise UpdateError(msg) from exc


def _parse_release(data: _ReleasePayload) -> Release:
    tag = data.get("tag_name", "")
    assets = tuple(
        Asset(
            name=asset["name"],
            download_url=asset["browser_download_url"],
            size=int(asset.get("size", 0)),
            digest=asset.get("digest", ""),
        )
        for asset in data.get("assets", [])
    )
    return Release(
        tag=tag,
        version=tag.removeprefix("v"),
        html_url=data.get("html_url", ""),
        assets=assets,
    )


def select_asset(release: Release) -> Asset | None:
    """Pick the release asset matching the current OS and architecture."""
    suffix = _platform_suffix()
    return next((asset for asset in release.assets if asset.suffix == suffix), None)


def _platform_suffix() -> str:
    match sys.platform:
        case "win32":
            return "windows-amd64.exe"
        case "darwin":
            return "macos-arm64"
        case _:
            return "linux-x86_64"


def compare_versions(current: str, latest: str) -> int:
    """Compare versions per PEP 440.

    Returns positive when ``current`` is newer than ``latest``, zero when
    equal, negative when ``current`` is older. Dev segments (``0.1.6.dev1``)
    and local segments (``+g<hash>``) are handled correctly, unlike a naive
    split-and-compare which falls back to lexicographic order on those.

    Falls back to lexicographic comparison when either string is not a
    valid PEP 440 version, so legacy data sources cannot crash the updater.
    """

    def _parse(version: str) -> Version | None:
        try:
            return Version(version)
        except InvalidVersion:
            return None

    parsed_current = _parse(current)
    parsed_latest = _parse(latest)
    if parsed_current is not None and parsed_latest is not None:
        if parsed_current > parsed_latest:
            return 1
        if parsed_current < parsed_latest:
            return -1
        return 0
    if latest != current:
        return (current > latest) - (current < latest)
    return 0


def download_to(asset: Asset, dest: Path) -> None:
    """Stream an asset to a file and verify size + sha256 before committing.

    The download is staged to ``<dest>.part`` and only renamed onto
    ``dest`` after both the byte count and the SHA-256 match what the
    GitHub Release payload advertised. A mismatch (truncated transfer,
    mirror mismatch, MITM) leaves the part file unlinked and raises
    ``UpdateError`` instead of installing a half-downloaded binary.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".part")
    sha256 = hashlib.sha256()
    bytes_downloaded = 0
    committed = False
    try:
        with urllib.request.urlopen(  # noqa: S310 -- release assets are HTTPS URLs.
            asset.download_url, timeout=REQUEST_TIMEOUT
        ) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                output.write(chunk)
                sha256.update(chunk)
                bytes_downloaded += len(chunk)
        if asset.size and bytes_downloaded != asset.size:
            raise UpdateError(
                f"size mismatch: expected {asset.size} bytes, got {bytes_downloaded}"
            )
        if asset.digest:
            expected = asset.digest.removeprefix("sha256:")
            actual = sha256.hexdigest()
            if actual != expected:
                raise UpdateError(
                    f"sha256 mismatch: expected {expected[:16]}..., got {actual[:16]}..."
                )
        _ = temporary.replace(dest)
        committed = True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as exc:
        msg = f"download failed: {exc}"
        raise UpdateError(msg) from exc
    finally:
        if not committed:
            temporary.unlink(missing_ok=True)


def replace_binary(new_path: Path, target: Path) -> str | None:
    """Replace ``target`` with ``new_path``. Return None on success or an
    error description (suitable for printing) on failure.

    The string distinguishes a Windows file lock from a generic OSError so
    the CLI can recommend the right recovery (kill running process vs.
    check permissions / antivirus).
    """
    try:
        _ = new_path.replace(target)
    except PermissionError as exc:
        return f"permission denied: {exc}"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def find_blocking_processes(
    binary_path: Path, *, exclude_pid: int | None = None
) -> list[int]:
    """Return PIDs of running processes whose image name matches the binary.

    Uses ``tasklist`` on Windows (the only platform where executables are
    locked while running). Returns an empty list on other platforms or when
    the lookup cannot be performed, so the caller can fall back to a manual
    message without crashing. ``exclude_pid`` lets the caller skip its own
    process so an in-place update does not suicide before reporting success.
    """
    if sys.platform != "win32":
        return []
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    target_name = binary_path.name.lower()
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip().strip('"') for part in line.split(",")]
        if len(parts) < 2 or parts[0].lower() != target_name:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if exclude_pid is not None and pid == exclude_pid:
            continue
        pids.append(pid)
    return pids


def kill_process(pid: int) -> bool:
    """Force-kill the process with the given PID. Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


def current_binary_path() -> Path | None:
    """Return the running cgate binary path, or ``None`` in development mode."""
    executable = Path(sys.executable).resolve()
    return executable if executable.name.startswith("cgate") else None

"""Self-update via GitHub Releases API. Pure logic; the CLI layer wraps it."""

from __future__ import annotations

import json
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from packaging.version import InvalidVersion, Version
from pathlib import Path
from typing import Final, NotRequired, TypedDict

DEFAULT_REPO: Final = "wanderlp/command-gate"
GITHUB_API: Final = "https://api.github.com"
REQUEST_TIMEOUT: Final = 15


class _AssetPayload(TypedDict):
    name: str
    browser_download_url: str
    size: NotRequired[int]


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
    """Stream an asset to a temporary file and atomically rename it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(  # noqa: S310 -- release assets are HTTPS URLs.
            asset.download_url, timeout=REQUEST_TIMEOUT
        ) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        _ = temporary.replace(dest)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as exc:
        temporary.unlink(missing_ok=True)
        msg = f"download failed: {exc}"
        raise UpdateError(msg) from exc


def replace_binary(new_path: Path, target: Path) -> bool:
    """Atomically replace ``target`` and report whether the operation succeeded."""
    try:
        _ = new_path.replace(target)
    except OSError:
        return False
    return True


def current_binary_path() -> Path | None:
    """Return the running cgate binary path, or ``None`` in development mode."""
    executable = Path(sys.executable).resolve()
    return executable if executable.name.startswith("cgate") else None

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cgate.update import (
    Asset,
    Release,
    UpdateError,
    _parse_release,
    _platform_suffix,
    compare_versions,
    current_binary_path,
    download_to,
    fetch_latest_release,
    replace_binary,
    select_asset,
)


def test_parse_release_extracts_assets_and_version() -> None:
    release = _parse_release(
        {
            "tag_name": "v1.2.3",
            "html_url": "https://example.test/release",
            "assets": [
                {
                    "name": "cgate-linux-x86_64",
                    "browser_download_url": "https://example.test/binary",
                    "size": 42,
                }
            ],
        }
    )

    assert release.version == "1.2.3"
    assert release.assets == (
        Asset("cgate-linux-x86_64", "https://example.test/binary", 42),
    )


def test_compare_versions_returns_positive_when_current_is_newer() -> None:
    assert compare_versions("0.1.5", "0.1.0") == 1


def test_compare_versions_returns_negative_when_current_is_older() -> None:
    assert compare_versions("0.1.0", "0.1.5") == -1


def test_compare_versions_returns_zero_when_equal() -> None:
    assert compare_versions("0.1.0", "0.1.0") == 0


def test_compare_versions_handles_pep440_dev_segments() -> None:
    # A dev release of the next minor is newer than the previous release.
    assert compare_versions("0.1.6.dev1", "0.1.5") == 1
    assert compare_versions("0.1.5", "0.1.6.dev1") == -1


def test_compare_versions_handles_local_segments() -> None:
    assert compare_versions("0.1.6.dev1+gabc1234", "0.1.5") == 1
    assert compare_versions("0.1.5", "0.1.6.dev1+gabc1234") == -1


def test_select_asset_picks_matching_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cgate.update._platform_suffix", lambda: "linux-x86_64")
    expected = Asset("cgate-linux-x86_64", "https://example.test/linux", 10)
    release = Release("v1", "1", "https://example.test", (expected,))

    assert select_asset(release) == expected


def test_select_asset_returns_none_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cgate.update._platform_suffix", lambda: "macos-arm64")
    release = Release(
        "v1",
        "1",
        "https://example.test",
        (Asset("cgate-linux-x86_64", "https://example.test/linux", 10),),
    )

    assert select_asset(release) is None


def test_current_binary_path_returns_path_when_running_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("C:/fake/cgate.exe")
    monkeypatch.setattr(sys, "executable", str(executable))

    assert current_binary_path() == executable.resolve()


def test_download_to_streams_to_temp_and_replaces(tmp_path: Path) -> None:
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(b"new binary")
    asset = Asset("cgate-linux-x86_64", "https://example.test/binary", 10)
    destination = tmp_path / "cgate"

    with patch("urllib.request.urlopen", return_value=response):
        download_to(asset, destination)

    assert destination.read_bytes() == b"new binary"
    assert not destination.with_suffix(".part").exists()


def test_replace_binary_succeeds_on_non_windows(tmp_path: Path) -> None:
    new_path = tmp_path / "cgate.new"
    target = tmp_path / "cgate"
    new_path.write_bytes(b"new")
    target.write_bytes(b"old")

    assert replace_binary(new_path, target) is True
    assert target.read_bytes() == b"new"


def test_fetch_latest_release_raises_update_error_on_http_error() -> None:
    error = urllib.error.HTTPError(
        "https://example.test", 500, "server error", hdrs=None, fp=None
    )

    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(UpdateError, match="failed to fetch"),
    ):
        fetch_latest_release()


def test_platform_suffix_returns_windows_for_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    assert _platform_suffix() == "windows-amd64.exe"

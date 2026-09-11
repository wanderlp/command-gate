from __future__ import annotations

import hashlib
import io
import json
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
    find_blocking_processes,
    find_mcp_serving_pids,
    kill_process,
    replace_binary,
    select_asset,
)


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_parse_release_extracts_assets_and_version() -> None:
    release = _parse_release(
        {
            "tag_name": "v1.2.3",
            "html_url": "https://example.test/release",
            "assets": [
                {
                    "name": "cgate-linux-x86_64",
                    "browser_download_url": "https://github.com/binary",
                    "size": 42,
                    "digest": _sha256_digest(b"placeholder"),
                }
            ],
        }
    )

    assert release.version == "1.2.3"
    assert release.assets == (
        Asset(
            "cgate-linux-x86_64",
            "https://github.com/binary",
            42,
            _sha256_digest(b"placeholder"),
        ),
    )


def test_parse_release_handles_missing_digest() -> None:
    release = _parse_release(
        {
            "tag_name": "v1",
            "html_url": "https://x",
            "assets": [
                {
                    "name": "cgate-linux-x86_64",
                    "browser_download_url": "https://x/b",
                    "size": 1,
                }
            ],
        }
    )
    assert release.assets[0].digest == ""


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
    expected = Asset("cgate-linux-x86_64", "https://example.test/linux", 10, "")
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
        (Asset("cgate-linux-x86_64", "https://example.test/linux", 10, ""),),
    )

    assert select_asset(release) is None


def test_current_binary_path_returns_path_when_running_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path("C:/fake/cgate.exe")
    monkeypatch.setattr(sys, "executable", str(executable))

    assert current_binary_path() == executable.resolve()


def test_download_to_refuses_untrusted_host(tmp_path: Path) -> None:
    """issue #14: an asset URL pointed off the allow-list must never be fetched."""
    asset = Asset("cgate-linux-x86_64", "https://evil.example/binary", 1, "")
    destination = tmp_path / "cgate"

    with (
        patch("urllib.request.urlopen") as urlopen,
        pytest.raises(UpdateError, match="untrusted host"),
    ):
        download_to(asset, destination)

    urlopen.assert_not_called()
    assert not destination.exists()


def test_download_to_refuses_non_https_scheme(tmp_path: Path) -> None:
    asset = Asset("cgate-linux-x86_64", "http://github.com/binary", 1, "")

    with (
        patch("urllib.request.urlopen") as urlopen,
        pytest.raises(UpdateError, match="untrusted host"),
    ):
        download_to(asset, tmp_path / "cgate")

    urlopen.assert_not_called()


def test_download_to_streams_to_temp_and_replaces(tmp_path: Path) -> None:
    payload = b"new binary contents"
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(payload)
    # No digest -> verification is skipped, so this stays simple.
    asset = Asset("cgate-linux-x86_64", "https://github.com/binary", len(payload), "")
    destination = tmp_path / "cgate"

    with patch("urllib.request.urlopen", return_value=response):
        download_to(asset, destination)

    assert destination.read_bytes() == payload
    assert not destination.with_suffix(".part").exists()


def test_download_to_raises_when_size_mismatches(tmp_path: Path) -> None:
    payload = b"twelve byte"  # 11 bytes
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(payload)
    # Advertise 99 bytes -- mismatch must abort before swap.
    asset = Asset("cgate-linux-x86_64", "https://github.com/binary", 99, "")
    destination = tmp_path / "cgate"

    with (
        patch("urllib.request.urlopen", return_value=response),
        pytest.raises(UpdateError, match="size mismatch"),
    ):
        download_to(asset, destination)

    assert not destination.exists()
    assert not destination.with_suffix(".part").exists()


def test_download_to_raises_when_sha256_mismatches(tmp_path: Path) -> None:
    payload = b"the actual bytes"
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(payload)
    wrong_digest = "sha256:" + ("0" * 64)
    asset = Asset(
        "cgate-linux-x86_64",
        "https://github.com/binary",
        len(payload),
        wrong_digest,
    )
    destination = tmp_path / "cgate"

    with (
        patch("urllib.request.urlopen", return_value=response),
        pytest.raises(UpdateError, match="sha256 mismatch"),
    ):
        download_to(asset, destination)

    assert not destination.exists()
    assert not destination.with_suffix(".part").exists()


def test_download_to_succeeds_when_sha256_matches(tmp_path: Path) -> None:
    payload = b"verified payload"
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(payload)
    asset = Asset(
        "cgate-linux-x86_64",
        "https://github.com/binary",
        len(payload),
        _sha256_digest(payload),
    )
    destination = tmp_path / "cgate"

    with patch("urllib.request.urlopen", return_value=response):
        download_to(asset, destination)

    assert destination.read_bytes() == payload


def test_replace_binary_succeeds_on_non_windows(tmp_path: Path) -> None:
    new_path = tmp_path / "cgate.new"
    target = tmp_path / "cgate"
    new_path.write_bytes(b"new")
    target.write_bytes(b"old")

    assert replace_binary(new_path, target) is None
    assert target.read_bytes() == b"new"


def test_replace_binary_returns_error_string_on_failure(tmp_path: Path) -> None:
    new_path = tmp_path / "cgate.new"
    target = tmp_path / "cgate"
    new_path.write_bytes(b"new")

    # Force Path.replace to raise PermissionError (simulates Windows file lock).
    real_replace = Path.replace

    def fake_replace(self: Path, target: Path) -> Path:
        if self == new_path:
            raise PermissionError(13, "locked", str(target))
        return real_replace(self, target)

    with patch.object(Path, "replace", fake_replace):
        result = replace_binary(new_path, target)

    assert result is not None
    assert "permission denied" in result.lower()


def test_find_blocking_processes_parses_tasklist_csv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    csv = (
        '"cgate.exe","1234","Console","1","12,345 K"\r\n'
        '"other.exe","5678","Console","1","1,234 K"\r\n'
        '"cgate.exe","9012","Console","1","9,999 K"\r\n'
        "\r\n"
    )
    completed = MagicMock()
    completed.stdout = csv
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed):
        pids = find_blocking_processes(tmp_path / "cgate.exe")

    assert pids == [1234, 9012]


def test_find_blocking_processes_returns_empty_on_non_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with patch("subprocess.run") as run:
        pids = find_blocking_processes(tmp_path / "cgate")

    assert pids == []
    run.assert_not_called()


def test_find_blocking_processes_returns_empty_when_tasklist_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with patch("subprocess.run", side_effect=FileNotFoundError):
        pids = find_blocking_processes(tmp_path / "cgate.exe")

    assert pids == []


def test_find_mcp_serving_pids_filters_by_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    rows = [
        {"ProcessId": 1234, "CommandLine": "C:\\bin\\cgate.exe mcp serve"},
        {"ProcessId": 5678, "CommandLine": "C:\\bin\\cgate.exe update apply"},
        {"ProcessId": 9012, "CommandLine": None},
    ]
    completed = MagicMock()
    completed.stdout = json.dumps(rows)
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed):
        pids = find_mcp_serving_pids([1234, 5678, 9012])

    assert pids == [1234]


def test_find_mcp_serving_pids_handles_single_object_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    completed = MagicMock()
    completed.stdout = json.dumps({"ProcessId": 42, "CommandLine": "cgate.exe mcp serve"})
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed):
        pids = find_mcp_serving_pids([42])

    assert pids == [42]


def test_find_mcp_serving_pids_ignores_pids_not_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    completed = MagicMock()
    completed.stdout = json.dumps({"ProcessId": 42, "CommandLine": "cgate.exe mcp serve"})
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed):
        pids = find_mcp_serving_pids([1234])

    assert pids == []


def test_find_mcp_serving_pids_returns_empty_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with patch("subprocess.run") as run:
        pids = find_mcp_serving_pids([1234])

    assert pids == []
    run.assert_not_called()


def test_find_mcp_serving_pids_returns_empty_when_no_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with patch("subprocess.run") as run:
        pids = find_mcp_serving_pids([])

    assert pids == []
    run.assert_not_called()


def test_find_mcp_serving_pids_returns_empty_when_powershell_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with patch("subprocess.run", side_effect=FileNotFoundError):
        pids = find_mcp_serving_pids([1234])

    assert pids == []


def test_find_mcp_serving_pids_returns_empty_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    completed = MagicMock()
    completed.stdout = "not json"
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed):
        pids = find_mcp_serving_pids([1234])

    assert pids == []


def test_kill_process_runs_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    completed = MagicMock()
    completed.returncode = 0

    with patch("subprocess.run", return_value=completed) as run:
        ok = kill_process(4321)

    assert ok is True
    args = run.call_args.args[0]
    assert args[0] == "taskkill"
    assert "/F" in args
    assert "/PID" in args
    assert "4321" in args


def test_kill_process_returns_false_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    with patch("subprocess.run") as run:
        ok = kill_process(4321)

    assert ok is False
    run.assert_not_called()


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

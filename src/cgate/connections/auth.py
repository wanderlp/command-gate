"""Credential storage in OS keyring and Windows kerberos passthrough detection.

Credentials use service ``command-gate`` and key ``connection:{alias}``. The JSON
payload contains the username, password, and SSH key.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

KEYRING_SERVICE = "command-gate"


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """A credential retrieved from the OS keyring for a connection alias."""

    username: str
    password: str | None
    ssh_key: str | None


def _key_for(alias: str) -> str:
    return f"connection:{alias}"


def _serialize(username: str, password: str | None, ssh_key: str | None) -> str:
    return json.dumps(
        {"username": username, "password": password, "ssh_key": ssh_key},
        separators=(",", ":"),
    )


def _deserialize(raw: str) -> StoredCredential:
    data = json.loads(raw)
    return StoredCredential(
        username=str(data["username"]),
        password=data.get("password"),
        ssh_key=data.get("ssh_key"),
    )


def store_credential(
    alias: str, *, username: str, password: str | None, ssh_key: str | None
) -> None:
    """Store a connection credential in the native OS keyring."""
    keyring.set_password(
        KEYRING_SERVICE, _key_for(alias), _serialize(username, password, ssh_key)
    )


def get_credential(alias: str) -> StoredCredential | None:
    """Return the stored credential for an alias, or None if absent."""
    raw = keyring.get_password(KEYRING_SERVICE, _key_for(alias))
    if raw is None:
        return None
    return _deserialize(raw)


def remove_credential(alias: str) -> bool:
    """Delete a credential and report whether deletion succeeded."""
    try:
        keyring.delete_password(KEYRING_SERVICE, _key_for(alias))
    except (PasswordDeleteError, KeyringError):
        return False
    else:
        return True


def is_kerberos_available() -> bool:
    """Best-effort detect Windows domain membership for kerberos passthrough."""
    if sys.platform != "win32":
        return False
    executable = shutil.which("wmic")
    if executable is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 -- executable resolved by shutil.which
            [executable, "computersystem", "get", "PartOfDomain", "/value"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return "PartOfDomain=TRUE" in (result.stdout or "")

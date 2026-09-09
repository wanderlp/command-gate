"""Pytest configuration: register custom markers for this project."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers.

    `slow`: marks tests as slow (e.g., the PyInstaller build smoke test runs a
    full binary build, ~30-60s). Skip with `pytest -m "not slow"`.
    """
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (skip with `-m 'not slow'`)"
    )

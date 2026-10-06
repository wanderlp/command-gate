"""Decision handlers for approving, executing, and rejecting commands.

This module re-exports the shared command lifecycle (mark_approved +
execute_and_finalize + reject_one + approve_remaining + reject_remaining)
from ``cgate.executor.lifecycle`` so the watch app can keep using
``cgate.watch.approval.execute_and_finalize`` and friends without depending
on the new module name. The lifecycle itself lives in
``cgate.executor.lifecycle`` because the MCP server also needs it -- the
two callers cannot diverge their implementations or the CAS race the
lifecycle prevents comes right back through the back door.
"""
from __future__ import annotations

from cgate.executor.lifecycle import (
    CommandDisappearedError,
    ConnectionNotFoundError,
    approve_one,
    approve_remaining,
    execute_and_finalize,
    mark_approved,
    reject_one,
    reject_remaining,
)

__all__ = [
    "CommandDisappearedError",
    "ConnectionNotFoundError",
    "approve_one",
    "approve_remaining",
    "execute_and_finalize",
    "mark_approved",
    "reject_one",
    "reject_remaining",
]
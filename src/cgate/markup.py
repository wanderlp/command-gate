"""Markup-safe text helpers shared by the TUI and the CLI.

`rich.markup.escape` is incomplete: its regex only matches a ``[`` when
it looks like the start of a Rich markup tag (followed by a letter,
``#``, ``/`` or ``@``), and never escapes a bare ``]``. Strings that
contain unbalanced brackets — for example XPath filters
(``*[System[Provider[@Name='Eventlog']]]``), .NET type literals
(``[System.IO.File]``), regex character classes (``[a-z]``), or the
PowerShell type-cast operator (``[xml]$x``) — slip past it and Textual's
``Static(markup=True)`` then raises ``rich.errors.MarkupError`` when
trying to render them.

This module provides a comprehensive escape that always emits a literal
``[``, ``]`` or ``\\`` regardless of context, so any string can be safely
interpolated into a Rich markup string by every call site without
reasoning about what Rich's escape considers "tag-like".

Issue: https://github.com/wanderlp/command-gate-for-ai-agents/issues/33
"""

from __future__ import annotations


def escape(markup: str) -> str:
    """Escape ``[``, ``]`` and ``\\`` so the input is safe to interpolate into markup.

    Every literal ``[`` becomes ``\\[``, every literal ``]`` becomes ``\\]``,
    and every existing ``\\`` is doubled (so a pre-escaped input round-trips
    correctly instead of un-escaping). The result is always parseable as Rich
    markup: nothing in it is ever interpreted as a tag, regardless of what
    follows the bracket.

    Unlike :func:`rich.markup.escape`, this makes no assumptions about the
    surrounding context, so it is safe for arbitrary user-controlled input
    (commands, batch titles, connection aliases, server hostnames, ...).

    Args:
        markup: The raw string to escape.

    Returns:
        The escaped string. Every ``[`` is prefixed by ``\\``, every ``]`` is
        prefixed by ``\\``, and every ``\\`` is doubled.

    Examples:
        >>> escape("[bold]hello[/bold]")
        '\\\\[bold]hello\\\\[/bold]'
        >>> escape("[System.IO.File]")
        '\\\\[System.IO.File\\\\]'
        >>> escape("[a]")
        '\\\\[a\\\\]'
        >>> escape("[")
        '\\\\['
        >>> escape("]")
        '\\\\]'
        >>> escape("plain text")
        'plain text'
        >>> escape("")
        ''
    """
    # Order matters. Double any existing backslashes first; otherwise the
    # backslash we are about to prepend to each bracket would silently
    # un-escape the next character on the next round of parsing.
    return (
        markup.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


__all__ = ["escape"]
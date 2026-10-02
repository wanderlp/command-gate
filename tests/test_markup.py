"""Unit tests for ``cgate.markup.escape``.

These tests intentionally exercise edge cases that ``rich.markup.escape``
fails on, plus a property-style round-trip: anything we escape must
parse as Rich markup without raising.
"""

from __future__ import annotations

from rich.text import Text

from cgate.markup import escape


# --- basic shape ----------------------------------------------------------


def test_empty_string_passes_through() -> None:
    assert escape("") == ""


def test_plain_text_is_unchanged() -> None:
    assert escape("hello world") == "hello world"


def test_does_not_touch_unrelated_chars() -> None:
    assert escape("a.b_c-d=e/f:g;h,i.j!k?l+m#n") == "a.b_c-d=e/f:g;h,i.j!k?l+m#n"


# --- bracket-by-bracket coverage ------------------------------------------


def test_escapes_single_open_bracket() -> None:
    assert escape("[") == "\\["


def test_escapes_single_close_bracket() -> None:
    """Regression: `rich.markup.escape` never escapes a bare `]`."""
    assert escape("]") == "\\]"


def test_escapes_balanced_brackets() -> None:
    assert escape("[a]") == "\\[a\\]"


def test_escapes_unbalanced_brackets() -> None:
    assert escape("[a") == "\\[a"
    assert escape("a]") == "a\\]"


def test_escapes_nested_brackets() -> None:
    """Regression: the exact XPath shape that crashed the TUI."""
    s = "wevtutil qe 'System' /q:\"*[System[Provider[@Name='User32'] and (EventID=1074)]]\" /c:1"
    out = escape(s)
    assert "[System" in out
    assert "[@Name=" in out
    assert "*\\[" in out
    assert "@Name='User32'" in out
    assert "(EventID=1074)" in out
    parsed = Text.from_markup(out)
    assert parsed.plain.count("[") == s.count("[")


# --- backslash handling ---------------------------------------------------


def test_doubles_existing_backslashes() -> None:
    assert escape("a\\b") == "a\\\\b"


def test_doubles_backslash_before_bracket() -> None:
    """If `\\` already precedes `[`, escaping must not consume the backslash
    that should escape the bracket. Doubling `\\` first preserves that."""
    assert escape("\\[x]") == "\\\\\\[x\\]"


# --- the exact failure mode from the user's audit trail -------------------


def test_xpath_filter_does_not_crash_rich_parser() -> None:
    """Regression for issue #33: rendering a command with the
    ``*[System[Provider[@Name=...]]`` shape used to raise
    ``rich.errors.MarkupError`` when Textual re-rendered the row.
    """
    cmd = (
        "wevtutil qe 'System' /q:\"*[System[Provider[@Name='Microsoft-Windows-Eventlog']"
        " and (EventID=1074 or EventID=42 or EventID=6005 or EventID=6008 or EventID=6009)]]\""
        " /c:50 /rd:true /f:xml 2>$null | Out-File 'C:\\Windows\\Temp\\sys-shutdown.xml'"
    )
    escaped = escape(cmd)
    # The escaped output must parse as Rich markup without raising.
    text = Text.from_markup(escaped)
    assert len(text) > 0


def test_dotnet_type_literal_does_not_crash_rich_parser() -> None:
    cmd = "$content = [System.IO.File]::ReadAllText('C:\\Windows\\Temp\\ts-all.xml')"
    escaped = escape(cmd)
    Text.from_markup(escaped)  # would raise on the un-fixed path


def test_powershell_xml_cast_does_not_crash_rich_parser() -> None:
    cmd = "[xml]$xml = Get-Content 'foo.xml' -Raw"
    escaped = escape(cmd)
    Text.from_markup(escaped)


def test_regex_character_class_does_not_crash_rich_parser() -> None:
    cmd = '$regex = [regex]"[a-z]+"'
    escaped = escape(cmd)
    Text.from_markup(escaped)


# --- it really is a drop-in for "insert into markup" -----------------------


def test_escaped_string_is_safe_between_markup_tags() -> None:
    """Sandwich the escaped value between real markup tags; if a bracket
    leaked through, the parser would either error or mis-style the
    surrounding tags."""
    cmd = "[evil]injected[/evil]"
    raw = f"[bold]{escape(cmd)}[/bold]"
    text = Text.from_markup(raw)
    assert "injected" in text.plain
    assert text.plain.count("[") == cmd.count("[")
    bold_spans = [s for s in text.spans if s.style == "bold"]
    assert len(bold_spans) == 1
    assert bold_spans[0].start == 0


# --- defensive coverage: weird inputs -------------------------------------


def test_escapes_only_brackets_not_words() -> None:
    """`rich.markup.escape` would treat `[foo]` as a tag identifier and
    escape it. Our version escapes every bracket, even those followed by
    a letter -- which is the whole point (so we don't depend on Rich
    deciding what looks like a tag)."""
    assert escape("[foo]") == "\\[foo\\]"
    assert escape("[bar]") == "\\[bar\\]"
    assert escape("[baz][qux]") == "\\[baz\\]\\[qux\\]"


def test_escapes_bracket_followed_by_at_sign() -> None:
    """`[@Name=...]` is the very shape that triggered the original crash.
    """
    assert escape("[@Name='Eventlog']") == "\\[@Name='Eventlog'\\]"


def test_escapes_bracket_followed_by_digit() -> None:
    assert escape("[42]") == "\\[42\\]"


def test_round_trips_when_re_escaped() -> None:
    """Escaping twice is safe -- the doubly-escaped output is still valid
    markup that parses back to the once-escaped form (which itself parses
    back to the original literal)."""
    once = escape("[x]")
    twice = escape(once)
    Text.from_markup(twice)
    Text.from_markup(once)
    assert "[" not in once or "\\[" in once
    assert "[" not in twice or "\\[" in twice
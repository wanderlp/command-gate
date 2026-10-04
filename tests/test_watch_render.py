"""Unit tests for the pure markup helpers in cgate.watch.render."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.text import Text

from cgate.db.types import Batch, BatchId, Command, CommandId, CommandStatus, ServerType
from cgate.watch.render import (
    approver_badge,
    format_batch_header,
    format_command_detail,
    format_command_line,
    format_queue_summary,
    risk_warning,
    status_glyph,
)
from cgate.watch.theme import CGATE_THEME

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _command(  # noqa: PLR0913 - one param per Command field under test, all optional
    *,
    status: CommandStatus = CommandStatus.PENDING,
    result: str | None = None,
    approved_by: str | None = None,
    reason: str | None = None,
    risk_label: str | None = None,
    command: str = "uptime",
    claimed_at: datetime | None = None,
) -> Command:
    return Command(
        id=CommandId("c1"),
        batch_id=BatchId("b1"),
        position=0,
        server_alias="linux-1",
        server_type=ServerType.LINUX,
        command=command,
        status=status,
        result=result,
        approved_by=approved_by,
        created_at=_NOW,
        resolved_at=None,
        claimed_at=claimed_at,
        reason=reason,
        risk_label=risk_label,
    )


def _batch(*, title: str = "deploy", description: str | None = None) -> Batch:
    return Batch(
        id=BatchId("b1"),
        title=title,
        description=description,
        requested_by_agent="agent",
        created_at=_NOW,
        resolved_at=None,
    )


def test_approver_badge_empty_when_not_yet_approved() -> None:
    assert approver_badge(None) == ""


def test_approver_badge_marks_auto_execution() -> None:
    badge = approver_badge("auto:watch:wlopez")
    assert "auto" in badge
    assert "wlopez" not in badge


def test_approver_badge_shows_human_username() -> None:
    badge = approver_badge("wlopez")
    assert "wlopez" in badge
    assert "auto" not in badge


def test_approver_badge_escapes_markup_in_username() -> None:
    badge = approver_badge("[bold]evil[/bold]")
    assert "\\[bold\\]" in badge


def test_format_command_line_includes_reason_when_present() -> None:
    line = format_command_line(_command(reason="cleaning up disk space"))
    assert "cleaning up disk space" in line


def test_format_command_line_omits_reason_line_when_absent() -> None:
    line = format_command_line(_command(reason=None))
    assert "↳" not in line


def test_format_command_line_escapes_markup_in_command_text() -> None:
    line = format_command_line(_command(command="echo [bold]hi[/bold]"))
    assert "\\[bold\\]" in line


def test_format_command_detail_shows_full_untruncated_result() -> None:
    long_result = "x" * 500
    detail = format_command_detail(_command(status=CommandStatus.EXECUTED, result=long_result))
    assert long_result in detail


def test_format_command_detail_includes_reason_and_approver() -> None:
    detail = format_command_detail(
        _command(
            status=CommandStatus.EXECUTED,
            approved_by="auto:watch:wlopez",
            reason="restart the crashed service",
        )
    )
    assert "restart the crashed service" in detail
    assert "auto" in detail


def test_risk_warning_empty_when_not_flagged() -> None:
    assert risk_warning(None) == ""


def test_risk_warning_shows_the_label() -> None:
    warning = risk_warning("recursive force delete (rm -rf)")
    assert "RISKY" in warning
    assert "recursive force delete (rm -rf)" in warning


def test_risk_warning_escapes_markup_in_the_label() -> None:
    assert "\\[bold\\]" in risk_warning("[bold]evil[/bold]")


def test_risk_warning_uses_the_theme_error_color() -> None:
    """Rich markup can't reference Textual's $error CSS variable, so this
    must pull the same hex value from CGATE_THEME instead of a hardcoded
    Rich color name that could drift from the app's actual theme."""
    assert CGATE_THEME.error in risk_warning("some risky command")


def test_status_glyph_colors_come_from_the_theme() -> None:
    assert CGATE_THEME.warning in status_glyph(CommandStatus.PENDING)
    assert CGATE_THEME.success in status_glyph(CommandStatus.EXECUTED)
    assert CGATE_THEME.error in status_glyph(CommandStatus.FAILED)


def test_format_command_line_includes_risk_warning_regardless_of_status() -> None:
    """Item 3: the human should see the flag in both PROPOSE and AUTO --
    this only checks rendering, so it applies whatever status a risky
    command happens to be in (PENDING here)."""
    line = format_command_line(_command(risk_label="wipes/formats a disk"))
    assert "RISKY" in line
    assert "wipes/formats a disk" in line


def test_format_command_line_omits_risk_warning_when_not_flagged() -> None:
    assert "RISKY" not in format_command_line(_command(risk_label=None))


def test_format_command_line_shows_running_hint_while_approved() -> None:
    """Regression: approve_one used to run mark-approved + execute in one
    shot with no chance for the TUI to show this state at all."""
    line = format_command_line(_command(status=CommandStatus.APPROVED))
    assert "running" in line.lower()


def test_format_command_detail_includes_risk_warning() -> None:
    detail = format_command_detail(_command(risk_label="formats a block device"))
    assert "RISKY" in detail
    assert "formats a block device" in detail


def test_format_batch_header_escapes_markup_in_title() -> None:
    header = format_batch_header(_batch(title="[red]fake[/red] title"))
    assert "\\[red\\]" in header


def test_format_batch_header_uses_the_theme_primary_color() -> None:
    assert CGATE_THEME.primary in format_batch_header(_batch())


def test_format_queue_summary_empty_when_nothing_pending() -> None:
    assert format_queue_summary(pending_commands=0, waiting_batches=0) == ""


def test_format_queue_summary_shows_count_with_a_single_batch() -> None:
    """Regression: the old format_waiting_notice showed nothing at all
    with just one batch pending, even if it had several commands left."""
    summary = format_queue_summary(pending_commands=3, waiting_batches=0)
    assert "3 pending command" in summary
    assert "waiting" not in summary


def test_format_queue_summary_also_mentions_waiting_batches() -> None:
    summary = format_queue_summary(pending_commands=5, waiting_batches=2)
    assert "5 pending command" in summary
    assert "2 batch(es) waiting" in summary


def test_format_command_line_renders_user_xpath_command_without_markup_error() -> None:
    """Regression for issue #33: the exact ``wevtutil`` XPath command that
    crashed the user's ``cgate watch`` TUI. Both ``format_command_line``
    and the output of ``Static(markup=True)`` must round-trip without
    raising ``rich.errors.MarkupError``.
    """
    cmd_text = (
        "wevtutil qe 'System' /q:\"*[System[Provider[@Name='Microsoft-Windows-Kernel-Power' "
        "or @Name='User32' or @Name='Microsoft-Windows-Winlogon' or "
        "@Name='Microsoft-Windows-Eventlog'] and "
        "(EventID=1074 or EventID=42 or EventID=6005 or EventID=6008 or EventID=6009)]]\""
        " /c:50 /rd:true /f:xml 2>$null | Out-File 'C:\\Windows\\Temp\\sys-shutdown.xml'"
    )
    line = format_command_line(
        _command(
            status=CommandStatus.FAILED,
            command=cmd_text,
            result="--- stderr ---\nSome failure\n",
        )
    )
    Text.from_markup(line)


def test_format_command_detail_renders_user_xpath_command_without_markup_error() -> None:
    """Same regression as above but for the un-truncated detail modal."""
    cmd_text = (
        "wevtutil qe 'System' /q:\"*[System[Provider[@Name='User32'] and (EventID=1074)]]\" "
        "/c:1 /rd:true /f:xml 2>$null"
    )
    detail = format_command_detail(
        _command(
            status=CommandStatus.EXECUTED,
            command=cmd_text,
            result="<Event>...</Event>",
        )
    )
    Text.from_markup(detail)

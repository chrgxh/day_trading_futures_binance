"""Unit tests for utils/general.py — daily report helpers."""

from datetime import datetime, timezone
from unittest.mock import patch

from utils.general import _read_log_warnings_errors, send_daily_report_email


def _log_line(date_str: str, level: str, msg: str) -> str:
    return f"{date_str} 10:30:45.123 | {level:<8} | bot:_run:100 - {msg}"


_REPORT_DATE = datetime(2026, 5, 11, tzinfo=timezone.utc)

_PNL_ROWS = [
    {"symbol": "BTCUSDT", "realized_pnl": "4.5000", "commission": "0.2000", "net_pnl": "4.3000", "trade_count": 3},
    {"symbol": "TOTAL",   "realized_pnl": "4.5000", "commission": "0.2000", "net_pnl": "4.3000", "trade_count": 3},
]


# ---------------------------------------------------------------------------
# _read_log_warnings_errors
# ---------------------------------------------------------------------------

def test_returns_warning_error_critical_for_correct_date(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("\n".join([
        _log_line("2026-05-11", "INFO",     "normal info — should be ignored"),
        _log_line("2026-05-11", "WARNING",  "something degraded"),
        _log_line("2026-05-11", "ERROR",    "bad error"),
        _log_line("2026-05-11", "CRITICAL", "fatal"),
        _log_line("2026-05-10", "WARNING",  "yesterday — should be excluded"),
        _log_line("2026-05-12", "ERROR",    "tomorrow — should be excluded"),
    ]))

    lines = _read_log_warnings_errors(str(log), _REPORT_DATE)

    assert len(lines) == 3
    assert any("something degraded" in l for l in lines)
    assert any("bad error" in l for l in lines)
    assert any("fatal" in l for l in lines)
    assert not any("yesterday" in l for l in lines)
    assert not any("tomorrow" in l for l in lines)
    assert not any("normal info" in l for l in lines)


def test_returns_empty_list_for_missing_file():
    lines = _read_log_warnings_errors("/nonexistent/path/bot.log", _REPORT_DATE)
    assert lines == []


def test_returns_empty_list_when_no_matching_lines(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(_log_line("2026-05-11", "INFO", "all quiet") + "\n")
    assert _read_log_warnings_errors(str(log), _REPORT_DATE) == []


# ---------------------------------------------------------------------------
# send_daily_report_email
# ---------------------------------------------------------------------------

def test_skips_when_env_vars_missing(tmp_path, monkeypatch):
    for key in ("RESEND_API_KEY", "CRASH_NOTIFY_EMAIL", "CRASH_NOTIFY_FROM_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    result = send_daily_report_email(_PNL_ROWS, str(tmp_path / "bot.log"), _REPORT_DATE)
    assert result is None


def test_sends_email_and_returns_id(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("CRASH_NOTIFY_EMAIL", "to@example.com")
    monkeypatch.setenv("CRASH_NOTIFY_FROM_EMAIL", "from@example.com")

    with patch("resend.Emails.send", return_value={"id": "abc-123"}) as mock_send:
        result = send_daily_report_email(_PNL_ROWS, str(tmp_path / "bot.log"), _REPORT_DATE)

    assert result == "abc-123"
    payload = mock_send.call_args[0][0]
    assert "2026-05-11" in payload["subject"]
    assert "4.3000" in payload["subject"]
    assert "<table" in payload["html"]
    assert "BTCUSDT" in payload["html"]
    assert "TOTAL" in payload["html"]


def test_email_shows_no_warnings_message_when_log_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("CRASH_NOTIFY_EMAIL", "to@example.com")
    monkeypatch.setenv("CRASH_NOTIFY_FROM_EMAIL", "from@example.com")

    log = tmp_path / "bot.log"
    log.write_text(_log_line("2026-05-11", "INFO", "quiet day") + "\n")

    with patch("resend.Emails.send", return_value={"id": "x"}) as mock_send:
        send_daily_report_email(_PNL_ROWS, str(log), _REPORT_DATE)

    html = mock_send.call_args[0][0]["html"]
    assert "No warnings or errors" in html


def test_email_includes_log_warnings_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("CRASH_NOTIFY_EMAIL", "to@example.com")
    monkeypatch.setenv("CRASH_NOTIFY_FROM_EMAIL", "from@example.com")

    log = tmp_path / "bot.log"
    log.write_text(_log_line("2026-05-11", "WARNING", "disk almost full") + "\n")

    with patch("resend.Emails.send", return_value={"id": "x"}) as mock_send:
        send_daily_report_email(_PNL_ROWS, str(log), _REPORT_DATE)

    html = mock_send.call_args[0][0]["html"]
    assert "disk almost full" in html
    assert "1 entries" in html


def test_returns_none_on_resend_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("CRASH_NOTIFY_EMAIL", "to@example.com")
    monkeypatch.setenv("CRASH_NOTIFY_FROM_EMAIL", "from@example.com")

    with patch("resend.Emails.send", side_effect=RuntimeError("network error")):
        result = send_daily_report_email(_PNL_ROWS, str(tmp_path / "bot.log"), _REPORT_DATE)

    assert result is None

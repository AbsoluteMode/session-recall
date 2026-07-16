from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from session_recall.timefmt import date_range_to_epoch, humanize_ts


def test_humanize_ts_absolute_and_relative():
    now = 1_700_000_000
    assert humanize_ts(0, now) == ""              # unknown ts (e.g. grep hits) -> empty
    assert "just now" in humanize_ts(now, now)
    assert "5m ago" in humanize_ts(now - 300, now)
    assert "3h ago" in humanize_ts(now - 3 * 3600, now)
    assert "2d ago" in humanize_ts(now - 2 * 86400, now)
    full = humanize_ts(now, now)
    assert "UTC" in full and full.startswith("20")  # ISO-ish absolute stamp present


def test_date_range_uses_inclusive_local_dates():
    zone = ZoneInfo("Asia/Yekaterinburg")
    start, end = date_range_to_epoch(
        "2026-07-14", "2026-07-14", "Asia/Yekaterinburg")

    assert start == int(datetime(2026, 7, 14, tzinfo=zone).timestamp())
    assert end == int(datetime(2026, 7, 15, tzinfo=zone).timestamp())
    assert end - start == 24 * 3600

    assert date_range_to_epoch(
        timezone_name="Asia/Yekaterinburg", on_date="2026-07-14") == (start, end)


def test_date_range_defaults_to_computer_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Yekaterinburg")
    assert date_range_to_epoch(on_date="2026-07-14") == date_range_to_epoch(
        on_date="2026-07-14", timezone_name="Asia/Yekaterinburg")


def test_date_range_respects_dst_and_open_ends():
    start, end = date_range_to_epoch(
        "2026-03-29", "2026-03-29", "Europe/Berlin")
    assert end - start == 23 * 3600
    assert date_range_to_epoch("2026-03-29", None, "UTC")[1] is None
    assert date_range_to_epoch(None, "2026-03-29", "UTC")[0] is None


@pytest.mark.parametrize(
    "start,end,zone,match",
    [
        ("2026-07-15", "2026-07-14", "UTC", "on or before"),
        ("07/14/2026", None, "UTC", "YYYY-MM-DD"),
        ("2026-07-14", None, "Mars/Olympus", "unknown IANA timezone"),
    ],
)
def test_date_range_rejects_invalid_input(start, end, zone, match):
    with pytest.raises(ValueError, match=match):
        date_range_to_epoch(start, end, zone)


def test_on_date_rejects_range_combination():
    with pytest.raises(ValueError, match="cannot be combined"):
        date_range_to_epoch("2026-07-14", None, "UTC", on_date="2026-07-14")

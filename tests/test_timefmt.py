from session_recall.timefmt import humanize_ts


def test_humanize_ts_absolute_and_relative():
    now = 1_700_000_000
    assert humanize_ts(0, now) == ""              # unknown ts (e.g. grep hits) -> empty
    assert "just now" in humanize_ts(now, now)
    assert "5m ago" in humanize_ts(now - 300, now)
    assert "3h ago" in humanize_ts(now - 3 * 3600, now)
    assert "2d ago" in humanize_ts(now - 2 * 86400, now)
    full = humanize_ts(now, now)
    assert "UTC" in full and full.startswith("20")  # ISO-ish absolute stamp present

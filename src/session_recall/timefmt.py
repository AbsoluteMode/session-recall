from datetime import datetime, timezone


def humanize_ts(ts: int, now: int) -> str:
    """Render an epoch timestamp as 'YYYY-MM-DD HH:MM UTC (Nx ago)' for humans.

    The index stores `ts` as a raw epoch int; surfaced verbatim it reads as an
    opaque number, making it hard to tell "now" from "old". `now` is passed in
    (not read from the clock) so the formatting is deterministic and testable.
    ts == 0 (unknown — e.g. grep hits carry no timestamp) -> "" so callers can
    show nothing rather than a fake 1970 date.
    """
    if not ts:
        return ""
    stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    delta = max(0, now - ts)
    if delta < 60:
        rel = "just now"
    elif delta < 3600:
        rel = f"{delta // 60}m ago"
    elif delta < 86400:
        rel = f"{delta // 3600}h ago"
    else:
        rel = f"{delta // 86400}d ago"
    return f"{stamp} ({rel})"

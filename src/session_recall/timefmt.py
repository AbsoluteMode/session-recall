from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_timezone():
    """Best-effort host timezone, preferring an IANA zone over a fixed offset."""
    candidates: list[str] = []
    env_tz = os.environ.get("TZ", "").lstrip(":")
    if env_tz:
        candidates.append(env_tz)
    try:
        configured = Path("/etc/timezone").read_text().strip()
        if configured:
            candidates.append(configured)
    except OSError:
        pass
    resolved = os.path.realpath("/etc/localtime")
    marker = "/zoneinfo/"
    if marker in resolved:
        candidates.append(resolved.split(marker, 1)[1])
    for name in candidates:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return datetime.now().astimezone().tzinfo or timezone.utc


def date_range_to_epoch(
    start_date: str | None = None,
    end_date: str | None = None,
    timezone_name: str | None = None,
    on_date: str | None = None,
) -> tuple[int | None, int | None]:
    """Convert inclusive local calendar dates to a half-open epoch range.

    ``2026-07-14`` in ``Asia/Yekaterinburg`` becomes local midnight at the
    start and the following local midnight at the end. The half-open shape
    keeps SQL and raw-transcript filtering identical while ``ZoneInfo``
    handles DST boundaries correctly.
    """
    if on_date and (start_date or end_date):
        raise ValueError("on_date cannot be combined with start_date or end_date")
    if on_date:
        start_date = end_date = on_date
    if not start_date and not end_date:
        return None, None
    if timezone_name is None:
        zone = local_timezone()
    else:
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {timezone_name!r}") from exc

    def parse(value: str | None, name: str) -> date | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")
        return parsed

    first = parse(start_date, "start_date")
    last = parse(end_date, "end_date")
    if first and last and first > last:
        raise ValueError(
            f"start_date must be on or before end_date; got {start_date!r} > {end_date!r}")

    start_ts = int(datetime.combine(first, time.min, zone).timestamp()) if first else None
    end_ts = (
        int(datetime.combine(last + timedelta(days=1), time.min, zone).timestamp())
        if last else None
    )
    return start_ts, end_ts


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

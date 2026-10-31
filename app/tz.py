"""IST timezone helpers. IST is fixed UTC+05:30 (no DST) — a constant shift."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def to_ist(dt: datetime) -> datetime:
    """Convert a datetime (any tz or naive-UTC) to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def now_iso() -> str:
    """ISO-8601 trace timestamp with offset."""
    return datetime.now(tz=UTC).isoformat()


def parse_incoming(ts: str) -> datetime:
    """Parse a WhatsApp received_at like '2026-09-22T09:05:00+05:30'."""
    return datetime.fromisoformat(ts.strip())


def ist_day(ts: str) -> date:
    """Calendar day (IST) of an incoming message timestamp."""
    return to_ist(parse_incoming(ts)).date()


def trip_day(started_at_utc: str) -> date:
    """Calendar day (IST) of a trip started_at like '2026-09-12T02:30:00Z'."""
    dt = started_at_utc.strip()
    if dt.endswith("Z"):
        dt = dt[:-1] + "+00:00"
    return to_ist(datetime.fromisoformat(dt)).date()

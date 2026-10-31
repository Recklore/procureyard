"""Data access over the loaded CSV tables."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PayoutLine, PendingOp, Rider, Trip


def get_rider(s: Session, rider_id: str) -> Rider | None:
    return s.get(Rider, rider_id)


def trips_for_rider(s: Session, rider_id: str) -> list[Trip]:
    """All trips filed under this rider's normalized id, ordered by start time.
    Duplicate rows (same trip_id appearing twice) collapse into one in Python."""
    rows = s.execute(select(Trip).where(Trip.norm_rider_id == rider_id).order_by(Trip.started_at)).scalars().all()
    seen: set[str] = set()
    out = []
    for t in rows:
        if t.trip_id in seen or t.is_duplicate:
            if t.trip_id in seen:
                continue
        seen.add(t.trip_id)
        out.append(t)
    return out


def trips_for_day(s: Session, rider_id: str, day: date) -> list[Trip]:
    rows = (s.execute(select(Trip).where(Trip.norm_rider_id == rider_id, Trip.ist_day == day)
                      .order_by(Trip.started_at)).scalars().all())
    seen: set[str] = set()
    out = []
    for t in rows:
        if t.trip_id in seen:
            continue
        seen.add(t.trip_id)
        out.append(t)
    return out


def distinct_completed_trips_for_day(s: Session, rider_id: str, day: date) -> list[Trip]:
    return [t for t in trips_for_day(s, rider_id, day) if t.status == "completed"]


def trip_by_id(s: Session, trip_id: str) -> Trip | None:
    rows = s.execute(select(Trip).where(Trip.trip_id == trip_id)).scalars().all()
    if not rows:
        return None
    return rows[0]


def payout_line_for_trip(s: Session, trip_id: str) -> PayoutLine | None:
    rows = s.execute(select(PayoutLine).where(PayoutLine.trip_id == trip_id,
                                              PayoutLine.line_type == "trip")).scalars().all()
    return rows[0] if rows else None


def trip_lines_for_rider_day(s: Session, rider_id: str, day: date) -> dict[str, PayoutLine]:
    rows = (s.execute(select(PayoutLine).where(PayoutLine.norm_rider_id == rider_id,
                                               PayoutLine.payout_date == day,
                                               PayoutLine.line_type == "trip")).scalars().all())
    return {ln.trip_id: ln for ln in rows}


def penalty_lines(s: Session, rider_id: str, day: date) -> list[PayoutLine]:
    return list(s.execute(select(PayoutLine).where(PayoutLine.norm_rider_id == rider_id,
                                                   PayoutLine.payout_date == day,
                                                   PayoutLine.line_type == "cancellation_penalty")).scalars().all())


def incentive_lines(s: Session, rider_id: str, day: date) -> list[PayoutLine]:
    return list(s.execute(select(PayoutLine).where(PayoutLine.norm_rider_id == rider_id,
                                                   PayoutLine.payout_date == day,
                                                   PayoutLine.line_type == "daily_incentive")).scalars().all())


def pending_ops_for_rider(s: Session, rider_id: str) -> list[PendingOp]:
    return list(s.execute(select(PendingOp).where(PendingOp.rider_id == rider_id,
                                                  PendingOp.status == "pending")).scalars().all())

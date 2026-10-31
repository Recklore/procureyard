"""Payout policy math — pure deterministic code. The LLM never touches money.

Rules (docs/policy.md):
- Trip fare: 25 base + 6 per km after the first 2 km; surge multiplier applies to the
  whole fare (base + distance). Round to nearest rupee, 0.5 rounds up.
- Daily incentive: 150 for >= 12 trips in an IST calendar day.
- Penalty: -10 per rider-cancelled trip. Customer cancels earn nothing.
- Day = IST calendar day.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

BASE_FARE = Decimal(25)
PER_KM = Decimal(6)
FREE_KM = Decimal(2)
INCENTIVE_AMOUNT = 150
INCENTIVE_TRIPS = 12
PENALTY_AMOUNT = Decimal(10)

# --- data-quality normalizations discovered in Phase 0 (scripts/analyze_data.py) ---

_MAX_SANE_KM = Decimal(100)


def clean_distance(raw: Decimal | str | float) -> Decimal:
    """Corrupt CSV distances show as 5000/9100/1300 where the payment ledger used
    5.0/9.1/1.3 — a dropped decimal separator. Anything >= 100 km is that glitch
    (real distances in this dataset are < 20 km), so divide by 1000."""
    v = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    if v >= _MAX_SANE_KM:
        return v / 1000
    return v


def norm_rider_id(rider_id: str) -> str:
    """Trips.csv contains 64 rows with rider-id typos ('R7', 'r19'). Normalize to the
    R"%03d" form used everywhere else. No real collision: riders.csv only has R001..R040."""
    m = re.match(r"^[Rr]0*(\d+)$", rider_id.strip())
    if not m:
        return rider_id.strip().upper()
    return f"R{int(m.group(1)):03d}"


def round_rupee(raw: Decimal) -> int:
    """Round to nearest whole rupee; 0.5 rounds up (ROUND_HALF_UP)."""
    return int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def trip_fare(distance_km: Decimal | str | float, surge_multiplier: Decimal | str | float) -> int:
    dist = clean_distance(distance_km)
    surge = surge_multiplier if isinstance(surge_multiplier, Decimal) else Decimal(str(surge_multiplier))
    raw = (BASE_FARE + PER_KM * max(Decimal(0), dist - FREE_KM)) * surge
    return round_rupee(raw)


def incentive_for_completed_count(completed_trips: int) -> int:
    """150 if the rider completed >= 12 (distinct) trips that IST day, else 0."""
    return INCENTIVE_AMOUNT if completed_trips >= INCENTIVE_TRIPS else 0


def penalty_refund(penalty_lines: int, rider_cancels: int) -> int:
    """Duplicate-charged penalties: charged lines beyond the rider's own cancels."""
    extra = penalty_lines - rider_cancels
    return int(PENALTY_AMOUNT * max(0, extra))


def is_eligible_incentive_day(completed_trips: int) -> bool:
    return completed_trips >= INCENTIVE_TRIPS


def in_dispute_window(received_day: date, dispute_day: date, days: int = 7) -> bool:
    """Policy: only the last 7 days — counting today and the 6 days before it,
    anchored on the message's received_at (not the system clock)."""
    return received_day - __import__("datetime").timedelta(days=days - 1) <= dispute_day <= received_day

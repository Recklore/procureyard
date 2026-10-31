"""Deterministic dispute investigation. Pure code — the LLM never computes money.

investigate() takes one claim (kind + IST day and/or named trip_ids) for a bound rider
and returns items (each with a stable dispute_id) plus human-readable facts that the
reply templates (or LLM) use verbatim. Amounts are recomputed from policy.py at
resolution time — items carry evidence, not trust.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from . import policy, repo
from .models import Trip


@dataclass
class Item:
    id: str            # stable: "trip:T123456", "incentive:2026-09-21", "penalty_dup:2026-09-18"
    type: str          # underpaid | unpaid | incentive_missing | penalty_dup
    trip_id: str | None
    day: date | None
    expected: int
    actual: int
    diff: int          # owed rupees


@dataclass
class Facts:
    """Plain facts for the reply builder (no LLM creativity needed on numbers)."""
    trips_done: int | None = None                      # distinct completed trips that day
    incentive_paid: int | None = None                  # incentive line amount if paid, else None
    incentive_eligible: bool | None = None
    penalty_lines: int | None = None
    rider_cancels: int | None = None
    surge_multiplier: float | None = None
    trip_distance: float | None = None
    trip_status: str | None = None
    note: str | None = None
    trip_id: str | None = None
    expected: int | None = None
    actual: int | None = None


@dataclass
class Investigation:
    items: list[Item] = field(default_factory=list)
    facts: Facts = field(default_factory=Facts)
    blocked: str | None = None        # set if we cannot act: "not_yours" | "out_of_window" | "trip_not_found"
    owed: int = 0

    @property
    def has_items(self) -> bool:
        return bool(self.items)


def _trip_item(trip: Trip, paid: int | None, kind: str) -> Item | None:
    expected = policy.trip_fare(trip.distance_km, trip.surge_multiplier)
    actual = paid if paid is not None else 0
    diff = expected - actual
    if diff <= 0:
        return None
    if kind == "surge" and (trip.surge_multiplier or "1.0") == "1.0":
        return None  # a surge claim on a trip with surge 1.0 has nothing owed
    if kind == "distance" and (trip.surge_multiplier or "1.0") != "1.0":
        return None
    return Item(id=f"trip:{trip.trip_id}", type=("unpaid" if paid is None else "underpaid"),
                trip_id=trip.trip_id, day=trip.ist_day, expected=expected, actual=actual, diff=diff)


def _investigate_trip(s: Session, rider_id: str, trip_id: str, kind: str) -> Investigation:
    inv = Investigation()
    trip = repo.trip_by_id(s, trip_id)
    if trip is None:
        inv.blocked = "trip_not_found"
        return inv
    line = repo.payout_line_for_trip(s, trip_id)
    inv.facts.trip_id = trip.trip_id
    inv.facts.surge_multiplier = float(trip.surge_multiplier)
    inv.facts.trip_distance = float(policy.clean_distance(trip.distance_km))
    inv.facts.expected = policy.trip_fare(trip.distance_km, trip.surge_multiplier)
    inv.facts.actual = int(line.amount) if line else 0
    if trip.norm_rider_id != rider_id:
        # access control: the trip ledger belongs to a different account
        inv.blocked = "not_yours"
        return inv
    inv.facts.trip_status = trip.status
    if trip.status != "completed":
        if kind == "penalty" and trip.status == "cancelled_by_rider":
            inv.facts.note = "cancelled_penalty"
        else:
            inv.facts.note = "cancelled"
        return inv
    item = _trip_item(trip, int(line.amount) if line else None, kind)
    if item:
        inv.items.append(item)
        inv.owed += item.diff
    else:
        inv.facts.note = "paid_correct"
    return inv


def _investigate_day(s: Session, rider_id: str, day: date, kind: str) -> Investigation:
    inv = Investigation()
    trips = repo.trips_for_day(s, rider_id, day)
    lines = repo.trip_lines_for_rider_day(s, rider_id, day)

    topics_all = {"fare", "missing_payment", "surge", "distance"}
    if kind == "fare":
        kinds = {"fare", "missing_payment"}
    else:
        kinds = {kind}

    if kinds & topics_all:
        # day-scan the rider's completed trips for fare gaps (missing lines,
        # underpayment, unpaid). The claim kind tightens which gaps count:
        # 'surge' only counts trips whose surge != 1.0; 'distance' only plain trips.
        for t in trips:
            if t.status != "completed":
                continue
            paid = int(lines[t.trip_id].amount) if t.trip_id in lines else None
            item = _trip_item(t, paid, kind)
            if item:
                inv.items.append(item)
                inv.owed += item.diff

    if kind == "incentive":
        completed = [t for t in trips if t.status == "completed"]
        count = len(completed)  # trips_for_day already deduplicates trip_ids
        inv.facts.trips_done = count
        eligible = policy.is_eligible_incentive_day(count)
        inv.facts.incentive_eligible = eligible
        inc = repo.incentive_lines(s, rider_id, day)
        inv.facts.incentive_paid = int(inc[0].amount) if inc else None
        if eligible and not inc:
            item = Item(id=f"incentive:{day}", type="incentive_missing", trip_id=None, day=day,
                        expected=policy.INCENTIVE_AMOUNT, actual=0, diff=policy.INCENTIVE_AMOUNT)
            inv.items.append(item)
            inv.owed += item.diff
        elif inc and policy.INCENTIVE_AMOUNT in [int(x.amount) for x in inc]:
            inv.facts.note = "incentive_paid"

    if kind == "penalty":
        pls = repo.penalty_lines(s, rider_id, day)
        cancels = len([t for t in trips if t.status == "cancelled_by_rider"])
        inv.facts.penalty_lines = len(pls)
        inv.facts.rider_cancels = cancels
        refund = policy.penalty_refund(len(pls), cancels)
        if refund > 0:
            item = Item(id=f"penalty_dup:{day}", type="penalty_dup", trip_id=None, day=day,
                        expected=refund, actual=0, diff=refund)
            inv.items.append(item)
            inv.owed += item.diff
        elif len(pls) == cancels and cancels > 0:
            inv.facts.note = "penalty_correct"
    return inv


def investigate_claim(s: Session, rider_id: str, claim) -> Investigation:
    if claim.trip_ids:
        trip_id = claim.trip_ids[0]
        if claim.day:
            trip = repo.trip_by_id(s, trip_id)
            if trip is not None and trip.norm_rider_id == rider_id:
                return _investigate_trip(s, rider_id, trip_id, claim.kind)
            if trip is not None and trip.norm_rider_id != rider_id:
                # try day scan too — data can hold the trip under another id's file row
                day_inv = _investigate_day(s, rider_id, claim.day, claim.kind)
                if day_inv.items:
                    return day_inv
        return _investigate_trip(s, rider_id, trip_id, claim.kind)
    if claim.day:
        return _investigate_day(s, rider_id, claim.day, claim.kind)
    return Investigation(blocked="need_details")

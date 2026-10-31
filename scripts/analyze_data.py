"""Phase 0 v2: policy diff with distance/rider normalization + keyed lookups fixed."""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IST = timezone(timedelta(hours=5, minutes=30))


def ist_day(ts: str) -> date:
    dt = ts.strip()
    if dt.endswith("Z"):
        dt = dt[:-1] + "+00:00"
    parsed = datetime.fromisoformat(dt).replace(tzinfo=None if not dt.endswith("+00:00") else timezone.utc)
    parsed = datetime.fromisoformat(dt)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(IST).date()


def norm_rider(rid: str) -> str:
    """R7 -> R007, r19 -> R019, R003 -> R003."""
    m = re.match(r"^[Rr](0*)(\d+)$", rid.strip())
    if not m:
        return rid.strip().upper()
    return f"R{int(m.group(2)):03d}"


def clean_distance(d: str) -> Decimal:
    val = Decimal(d)
    if val >= 100:
        return val / 1000
    return val


def trip_fare(distance_km: Decimal, surge: Decimal) -> int:
    raw = (Decimal(25) + Decimal(6) * max(Decimal(0), distance_km - Decimal(2))) * surge
    return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def main() -> None:
    riders = list(csv.DictReader(open(DATA / "riders.csv")))
    trips = list(csv.DictReader(open(DATA / "trips.csv")))
    lines = list(csv.DictReader(open(DATA / "payout_lines.csv")))

    print("bad rider_ids in trips.csv:", sorted({norm_rider(t["rider_id"]) for t in trips if t["rider_id"] != norm_rider(t["rider_id"])}))
    print("normalized rider mismatch rows:", sum(1 for t in trips if t["rider_id"] != norm_rider(t["rider_id"])))
    big = [t for t in trips if Decimal(t["distance_km"]) >= 100]
    print("corrupt-distance rows (>=100):", len(big), "min:", min(Decimal(t['distance_km']) for t in big), "max:", max(Decimal(t['distance_km']) for t in big))
    mid = [t["distance_km"] for t in trips if 20 < Decimal(t["distance_km"]) < 100]
    print("distances in (20,100):", len(mid), mid[:20])

    for t in trips:
        t["nrider"] = norm_rider(t["rider_id"])
        t["_ist_day"] = ist_day(t["started_at"])
        t["_dist"] = clean_distance(t["distance_km"])
        t["_surge"] = Decimal(t["surge_multiplier"])

    trips_by_id = {t["trip_id"]: t for t in trips}
    trip_lines: dict[str, list] = defaultdict(list)
    penalties = defaultdict(list)
    incentives = defaultdict(list)
    for ln in lines:
        rid, pd = norm_rider(ln["rider_id"]), date.fromisoformat(ln["payout_date"])
        if ln["line_type"] == "trip":
            trip_lines[ln["trip_id"]].append(ln)
        elif ln["line_type"] == "cancellation_penalty":
            penalties[(rid, pd)].append(ln)
        elif ln["line_type"] == "daily_incentive":
            incentives[(rid, pd)].append(ln)

    completed = defaultdict(list)
    for t in sorted(trips, key=lambda t: t["started_at"]):
        if t["status"] == "completed":
            completed[(t["nrider"], t["_ist_day"])].append(t)

    fare_mismatch = []
    unpaid_completed = []
    for (rid, day), ts_ in sorted(completed.items()):
        for t in ts_:
            expected = trip_fare(t["_dist"], t["_surge"])
            ln = trip_lines.get(t["trip_id"])
            if ln is None:
                unpaid_completed.append((rid, str(day), t["trip_id"], expected))
                continue
            actual = int(ln[0]["amount"])
            if actual != expected:
                fare_mismatch.append({"rider": rid, "day": str(day), "trip": t["trip_id"],
                                      "expected": expected, "actual": actual, "diff": expected - actual,
                                      "rawdist": t["distance_km"], "dist": str(t["_dist"]), "surge": t["surge_multiplier"]})

    print(f"\nTRUE unpaid completed: {len(unpaid_completed)}")
    for r in unpaid_completed:
        print("  UNPAID", r)
    print(f"\nTRUE fare mismatches: {len(fare_mismatch)}")
    for m in fare_mismatch:
        print("  MISMATCH", m)

    pen_issues = []
    for (rid, day), pls in sorted(penalties.items()):
        cancels = sum(1 for t in trips if t["nrider"] == rid and t["status"] == "cancelled_by_rider" and t["_ist_day"] == day)
        if len(pls) != cancels:
            pen_issues.append({"rider": rid, "day": str(day), "lines": len(pls), "cancels": cancels})
    print(f"\nTrue penalty issues: {len(pen_issues)}")
    for p in pen_issues:
        print("  PEN", p)

    inc_issues = []
    for (rid, day), comp in sorted(completed.items()):
        paid = len(incentives.get((rid, day), []))
        n = len(comp)
        if n >= 12 and paid == 0:
            inc_issues.append({"rider": rid, "day": str(day), "trips": n, "issue": "MISSING"})
        elif n < 12 and paid > 0:
            inc_issues.append({"rider": rid, "day": str(day), "trips": n, "issue": "PAID WITHOUT ELIGIBILITY"})
        elif n >= 12 and paid > 1:
            inc_issues.append({"rider": rid, "day": str(day), "trips": n, "issue": "DUPLICATE"})
    print(f"\nTrue incentive issues: {len(inc_issues)}")
    for i in inc_issues:
        print("  INC", i)

    # key cross-check: incentive lines keyed by payout_date vs trip IST day
    print("\nincentive line dates:", sorted({(k[0], str(k[1])) for k in incentives})[:80])

    print("\n--- CLAIMED TRIPS ---")
    for tid in ["T482410", "T252921", "T990831", "T849302", "T502951", "T482410", "T980582", "T502951"]:
        t = trips_by_id.get(tid)
        if not t:
            print(f"  {tid}: NOT FOUND")
        else:
            ln = trip_lines.get(tid)
            exp = trip_fare(t["_dist"], t["_surge"])
            print(f"  {tid}: nrider={t['nrider']} status={t['status']} dist={t['_dist']} surge={t['_surge']} day={t['_ist_day']} expected={exp} paid={[x['amount'] for x in ln] if ln else None}")

    print("\n--- INCENTIVE claims: trips count vs payout grouped by both bucketings ---")
    for rid, day in [("R005", date(2026, 9, 21)), ("R024", date(2026, 9, 18)), ("R009", date(2026, 9, 17)), ("R011", date(2026, 9, 19)), ("R025", date(2026, 9, 19))]:
        comp = completed.get((rid, day), [])
        inc = incentives.get((rid, day), [])
        onc = [l for l in lines if norm_rider(l["rider_id"]) == rid and l["line_type"] == "daily_incentive"]
        print(f"  {rid} {day}: completed={len(comp)} incentive_lines_that_day={len(inc)} all_incentive_lines={[(l['payout_date'], l['amount']) for l in onc]}")

    print("\n--- R007 rider rows ---")
    for t in trips:
        if t["nrider"] == "R007" and t["_ist_day"] == date(2026, 9, 19):
            ln = trip_lines.get(t["trip_id"])
            print(f"  {t['trip_id']} rawrid={t['rider_id']} dist={t['_dist']} surge={t['_surge']} expected={trip_fare(t['_dist'], t['_surge'])} paid={[x['amount'] for x in ln] if ln else None}")


if __name__ == "__main__":
    main()

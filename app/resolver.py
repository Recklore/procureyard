"""Decision layer — pure code. Given clusters of verified items and the rider's
payout history it decides: auto-pay (≤₹200, once/rider/day), queue for ops approval,
or nothing. All amounts are recomputed HERE from policy math on the item evidence;
the LLM never writes a rupee figure into a decision.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PayoutLedger

AUTO_CAP = 200

KIND_ORDER = {"fare": 0, "missing_payment": 1, "surge": 2, "distance": 3, "incentive": 4, "penalty": 5}


def auto_payout_used_today(s: Session, rider_id: str, day: date) -> bool:
    """Once-per-rider-per-day auto-pay gate: any held/paid/failed auto row today (IST,
    anchored on the incoming message's received date) blocks another auto-pay."""
    return s.execute(
        select(PayoutLedger.id).where(PayoutLedger.rider_id == rider_id,
                                      PayoutLedger.day == day,
                                      PayoutLedger.decided_by == "auto",
                                      PayoutLedger.status.in_(["held", "paid", "failed"]))
    ).first() is not None


def decide_clusters(s: Session, rider_id: str, received_day: date, clusters: list[dict]) -> dict:
    """clusters: list of {kind, day, items: [Item]}.

    Returns {'actions': [...]} — each action is one of:
      {'type': 'auto_pay', 'amount', 'cluster'}
      {'type': 'approval', 'amount', 'cluster'}
    Order: the first eligible (≤₹200 & no auto today) cluster auto-pays; every
    remaining payable cluster queues for ops approval (finance rule: one auto-pay
    per rider per day; over-₹200 always needs ops, never a partial ₹200 payout).
    """
    fresh_clusters = []
    for c in clusters:
        amount = sum(item.diff for item in c["items"])
        if amount > 0:
            fresh_clusters = fresh_clusters or []
            fresh_clusters.append({**c, "amount": amount})
    # One dispute may contain multiple items (e.g. two unpaid trips named together);
    # separate clusters are separate disputes.
    fresh_clusters.sort(key=lambda c: (c.get("day") or received_day, KIND_ORDER.get(c.get("kind"), 9)))

    used = auto_payout_used_today(s, rider_id, received_day)
    actions = []
    for c in fresh_clusters:
        if c["amount"] <= AUTO_CAP and not used:
            actions.append({"type": "auto_pay", "amount": c["amount"], "cluster": c})
            used = True
        elif c["amount"] > 0:
            actions.append({"type": "approval", "amount": c["amount"], "cluster": c})
    return {"actions": actions}

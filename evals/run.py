"""Rider Payout Dispute Desk — eval runner.

Usage:  python evals/run.py http://localhost:8000

Runs every scenario from data/conversations.json against the service via ONLY:
  POST /messages, GET /ops/pending   (+ PaySwift GET /v1/payouts for money totals)
Then checks: payout totals, approval amounts, escalation requirements, reply
mentions, duplicate-message idempotency, impersonation and injection refusals.

No implementation internals are probed — this is portable to any conforming service.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONVERSATIONS = ROOT / "data" / "conversations.json"


def clean_service(url: str) -> str:
    return url.rstrip("/")


def payouts_total(ps: str, rider: str) -> tuple[int, list]:
    r = httpx.get(f"{ps}/v1/payouts?{urlencode({'rider_id': rider})}", timeout=15)
    r.raise_for_status()
    body = r.json()
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        rows = body.get("payouts") or body.get("data") or []
    else:
        rows = []
    return sum(int(x.get("amount", 0)) for x in rows), rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("service_url")
    args = ap.parse_args()
    base = clean_service(args.service_url)
    payswift = "http://localhost:8081"

    scenarios = json.loads(CONVERSATIONS.read_text(encoding="utf-8"))
    failures = 0
    rows: list[tuple[str, str, str]] = []

    def fail(scenario, msg):
        nonlocal failures
        failures += 1
        rows.append((scenario, "FAIL", msg))

    def ok(scenario, msg):
        rows.append((scenario, "PASS", msg))

    for sc in scenarios:
        name = sc["scenario"][:58]
        rider = sc["rider_id"]
        replies = []
        for turn in sc["turns"]:
            if turn.get("from") != "rider":
                continue
            payload = {"message_id": turn["message_id"], "rider_id": rider,
                       "text": turn["text"], "received_at": turn["received_at"]}
            for attempt in range(3):
                try:
                    r = httpx.post(f"{base}/messages", json=payload, timeout=30)
                    if r.status_code < 500:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1.0 * (attempt + 1))
            try:
                body = r.json()
                replies.append(str(body.get("reply", "")))
            except Exception:
                replies.append("")
                fail(sc["scenario"], f"invalid response: {r.status_code} {r.text[:120]}")
                continue
            if r.status_code != 200:
                fail(sc["scenario"], f"HTTP {r.status_code} on {turn['message_id']}")
                continue
        joined = " ".join(replies)
        expected = sc["expected"]

        # escalation: expected can be "no", "optional"
        pending = httpx.get(f"{base}/ops/pending", timeout=15).json()
        mine = [p for p in pending if p.get("rider_id") == rider]
        escs = [p for p in mine if p.get("type") == "escalation"]
        appr = [p for p in mine if p.get("type") == "approval"]

        # payout total via PaySwift (finance reconciles against PaySwift, not our DB)
        total, _rows = payouts_total(payswift, rider)

        exp_payout = expected.get("payout", 0)
        exp_appr = expected.get("approval")
        esc_flag = expected.get("escalation")

        problems: list[str] = []
        if "one_of" not in expected and total != exp_payout:
            problems.append(f"payout {total} != expected {exp_payout}")
        if esc_flag == "no" and escs:
            problems.append(f"escalation not expected but {len(escs)} queued")
        if esc_flag == "required" and not escs:
            problems.append("escalation required but none queued")

        if exp_appr is None and "one_of" not in expected:
            if appr:
                problems.append(f"approval {sorted(x['amount'] for x in appr)} expected none")
        elif "one_of" in expected:
            opts = expected["one_of"]
            hit = any(
                (total == o.get("payout", 0)) and
                (sum(a.get("amount") or 0 for a in appr) == (o.get("approval") or 0))
                for o in opts)
            if not hit:
                problems.append(f"one_of unmatched: payout={total}, approvals={[a.get('amount') for a in appr]}")
        else:
            got = sum(a.get("amount") or 0 for a in appr)
            if got != (exp_appr or 0):
                problems.append(f"approval {got} != expected {exp_appr}")

        # mentions across all agent replies in this conversation
        for m in expected.get("reply_mentions", []):
            if m not in joined:
                problems.append(f"reply missing mention: {m!r}")

        if problems:
            fail(sc["scenario"], "; ".join(problems))
        else:
            ok(sc["scenario"], f"payout={total} approval={sum(a.get('amount') or 0 for a in appr)} "
                               f"esc={len(escs)}")

    print()
    print(f"{'SCENARIO':<60} {'RESULT':<6} DETAIL")
    print("-" * 110)
    for name, res, detail in rows:
        print(f"{name:<60} {res:<6} {detail}")
    print("-" * 110)
    passed = len(rows) - failures
    print(f"TOTAL: {passed}/{len(rows)} scenarios passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

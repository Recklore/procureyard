"""Local scenario sweep against SQLite + stubbed PaySwift (no Docker needed)."""
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./test_qd.db"
os.environ["PAYSWIFT_BASE_URL"] = "http://localhost:8081"
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import load_csvs, session  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.models import PayoutLedger, PendingOp, Message, Conversation  # noqa: E402

load_csvs(ROOT / "data")


def clean():
    with session() as s:
        for q in (PayoutLedger, PendingOp, Message, Conversation):
            for r in s.query(q).all():
                s.delete(r)
        s.commit()


class PS:
    def __init__(self):
        self.calls = []

    async def payout(self, rid, amt, ref, key):
        self.calls.append((rid, amt))
        return {"payout_id": f"ps{len(self.calls)}", "status": "paid"}

    async def list_payouts(self, rider_id=None):
        return []


ps = PS()
a = Agent(payswift=ps)
scs = json.loads((ROOT / "data" / "conversations.json").read_text(encoding="utf-8"))
for i, sc in enumerate(scs):
    clean()
    ps.calls.clear()
    e = sc["expected"]
    for t in sc["turns"]:
        if t.get("from") != "rider":
            continue
        r = asyncio.new_event_loop().run_until_complete(
            a.handle(sc["rider_id"], t["message_id"], t["text"], t["received_at"]))
        reply = r[:150].replace("\n", " ")
        print(f"[{i}] {sc['rider_id']} << {t['text'][:68]!r}")
        print(f"    >> {reply}")
    with session() as s:
        pend = [(p.type, p.amount) for p in s.query(PendingOp).filter_by(
            rider_id=sc["rider_id"], status="pending").all()]
    paid_total = sum(x[1] for x in ps.calls)
    print("    payswift:", ps.calls, "pending:", pend,
          "| expected payout", e.get("payout"), "appr", e.get("approval"),
          "esc", e.get("escalation"))
    print()

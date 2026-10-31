"""FastAPI service: WhatsApp vendor interface + ops surface + dashboard."""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text

from . import policy, trace
from .db import data_ready, load_csvs, session
from .models import Message, PendingOp, PayoutLedger, TraceStep
from .payswift import PaySwift
from .tz import now_iso

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("qd.main")

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_payswift: PaySwift | None = None
_agent = None


def get_payswift() -> PaySwift:
    global _payswift
    if _payswift is None:
        _payswift = PaySwift(os.environ.get("PAYSWIFT_BASE_URL", "http://payswift:8081"))
    return _payswift


def get_agent():
    global _agent
    if _agent is None:
        from .agent import Agent
        _agent = Agent(payswift=get_payswift())
    return _agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_csvs(DATA_DIR)  # health gates on rows actually present
    yield
    if _payswift is not None:
        await _payswift.aclose()


app = FastAPI(title="QuickDrop Rider Payout Dispute Desk", lifespan=lifespan)


def _dump(steps) -> list[dict]:
    return [{"at": st.at.isoformat() if isinstance(st.at, datetime) else str(st.at),
             "type": st.type, "name": st.name,
             "input": json.loads(st.input) if st.input else None,
             "output": json.loads(st.output) if st.output else None}
            for st in steps]


# --------------------------------------------------------------------- vendor API
@app.post("/messages")
async def post_message(payload: dict):
    rider_id = str(payload.get("rider_id", "")).strip()
    message_id = str(payload.get("message_id", "")).strip()
    text_msg = str(payload.get("text", "")).strip()
    received_at = str(payload.get("received_at", "") or now_iso())
    if not rider_id or not message_id:
        raise HTTPException(status_code=400, detail="rider_id and message_id required")
    reply = await get_agent().handle(rider_id, message_id, text_msg, received_at)
    return {"reply": reply}


@app.get("/trace/{rider_id}")
async def get_trace(rider_id: str):
    rider_id = policy.norm_rider_id(rider_id)
    with session() as s:
        rows = s.execute(select(TraceStep).where(TraceStep.rider_id == rider_id)
                         .order_by(TraceStep.id)).scalars().all()
    return _dump(rows)


@app.get("/ops/pending")
async def ops_pending():
    with session() as s:
        rows = s.execute(select(PendingOp).where(PendingOp.status == "pending")
                         .order_by(PendingOp.id)).scalars().all()
        return [{"id": r.id, "rider_id": r.rider_id, "type": r.type,
                 "amount": r.amount, "reason": r.reason,
                 "created_at": r.created_at.isoformat() if isinstance(r.created_at, datetime) else str(r.created_at)}
                for r in rows]


def _ledger_row_for_key(s, idem_key: str | None):
    if not idem_key:
        return None
    return s.query(PayoutLedger).filter_by(idem_key=idem_key).one_or_none()


@app.post("/ops/approve/{op_id}")
async def ops_approve(op_id: int):
    """Approve → payout ONCE. The approval's ledger row was already written (status
    'queued') when the dispute was queued; approve flips it via the PaySwift call.
    Double click / retry → same response, no second payout (atomic status change +
    deterministic idempotency key). A retried 'failed' payout may be re-fired with
    the same key, and PaySwift dedupes it server-side."""
    # atomic pending→approved: exactly one caller wins the transition
    with session() as s:
        op = s.get(PendingOp, op_id)
        if op is None:
            raise HTTPException(status_code=404, detail="not found")
        if op.type == "escalation":
            raise HTTPException(status_code=400, detail="escalations have no payout")
        if op.status == "pending":
            updated = s.execute(text(
                "update pending_ops set status='approved', resolved_at=:now "
                "where id=:id and status='pending'"), {"now": now_iso(), "id": op_id}).rowcount
            if updated == 1:
                s.commit()
            else:
                s.rollback()  # lost the race: another caller approved first
        # (re-load op state after transition)
        with session() as s2:
            op = s2.get(PendingOp, op_id)
            ledger = _ledger_row_for_key(s2, op.idem_key)
            if ledger is not None and ledger.status == "paid":
                # already paid in an earlier call/click → no second payout
                return {"id": op.id, "status": "approved",
                        "payswift_payout_id": ledger.payswift_payout_id, "payout": {"duplicate": True}}
            if ledger is None:
                op_day = op.created_at.date() if op.created_at else date.today()
                ledger = PayoutLedger(rider_id=op.rider_id, day=op_day, amount=op.amount,
                                      covered_ids=op.covered_ids or "[]", decided_by="ops",
                                      status="held", idem_key=op.idem_key, pending_op_id=op.id)
                s2.add(ledger)
            else:
                ledger.status = "held"      # queued or failed → re-arm for payout
                ledger.decided_by = "ops"
            s2.commit()

    try:
        resp = await get_payswift().payout(op.rider_id, op.amount,
                                           f"ops approval #{op.id} | {op.rider_id}",
                                           f"qd-ops-{op.idem_key}")
    except Exception as e:  # noqa: BLE001 — operator needs the exact failure
        with session() as s:
            ledger = _ledger_row_for_key(s, op.idem_key)
            if ledger is not None and ledger.status != "paid":
                ledger.status = "failed"
            trace.record(s, op.rider_id, op.conversation_id, "error", "ops_approve",
                         {"op": op.id, "amount": op.amount}, {"error": str(e)})
            s.commit()
        raise HTTPException(status_code=502, detail=f"payswift error: {e}") from e

    with session() as s:
        ledger = _ledger_row_for_key(s, op.idem_key)
        if ledger is not None:
            ledger.status = "paid"
            ledger.payswift_payout_id = resp.get("payout_id")
        op2 = s.get(PendingOp, op_id)
        if op2 is not None:
            op2.payswift_payout_id = resp.get("payout_id")
        trace.record(s, op.rider_id, op.conversation_id, "tool_call", "ops_approve",
                     {"op": op.id, "amount": op.amount}, resp)
        s.commit()
        return {"id": op.id, "status": "approved", "payout": resp}


@app.post("/ops/reject/{op_id}")
async def ops_reject(op_id: int):
    with session() as s:
        op = s.get(PendingOp, op_id)
        if op is None:
            raise HTTPException(status_code=404, detail="not found")
        if op.status != "pending":
            return {"id": op.id, "status": op.status}
        updated = s.execute(text(
            "update pending_ops set status='rejected', resolved_at=:now where id=:id and status='pending'"),
            {"now": now_iso(), "id": op_id}).rowcount
        s.commit()
        if updated:
            s.execute(text("update payout_ledger set status='rejected' where pending_op_id=:id and status='queued'"),
                      {"id": op_id})
            s.commit()
    return {"id": op.id, "status": "rejected"}


# --------------------------------------------------------------------- misc
@app.get("/health")
async def health():
    if data_ready():
        return {"status": "ok"}
    raise HTTPException(status_code=503, detail="data not loaded")


@app.get("/ops", response_class=HTMLResponse)
async def ops_dashboard(request: Request, rider: str | None = None):
    with session() as s:
        msgs = s.execute(select(Message).order_by(Message.created_at)).scalars().all()
        riders: list[str] = []
        seen: set[str] = set()
        for m in msgs:
            if m.rider_id not in seen:
                seen.add(m.rider_id)
                riders.append(m.rider_id)
        pending = s.execute(select(PendingOp).order_by(PendingOp.id)).scalars().all()
        steps: list[dict] = []
        if rider:
            r = policy.norm_rider_id(rider)
            steps = _dump(s.execute(select(TraceStep).where(TraceStep.rider_id == r)
                                    .order_by(TraceStep.id)).scalars().all())
            rider = r
    return _templates.TemplateResponse(request, "ops.html", {
        "riders": riders, "pending": pending, "rider": rider, "trace": steps})

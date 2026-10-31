"""Agent orchestrator: one WhatsApp message → one deterministic pipeline.

  1. dedupe message_id (unique insert acts as the in-flight lock; duplicates poll for
     the original's reply instead of re-running the agent)
  2. deterministic intent parse (app/intent.py)
  3. claim-scoped investigation (app/engine.py — only what the rider referenced)
  4. code-owned resolution (app/resolver.py: auto ≤₹200 once/day, else ops approval)
  5. PaySwift call with a deterministic idempotency key, ledger row written first
  6. template reply (numbers only from investigation facts), optional LLM polish

Guardrails (all code-enforced):
  - rider_id is bound out-of-band from the message envelope, never from the text
  - no tool accepts an amount or a rider id from the model
  - a payout only ever happens for verified, not-yet-compensated discrepancies
  - covered discrepancy ids live in payout_ledger/PendingOp: the same trip/incentive
    can never be paid twice from later conversations or re-sends
  - once-per-rider-per-day auto-pay is measured in IST anchored on received_at
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import engine, intent, policy, replies, resolver, trace
from .db import session
from .models import Conversation, Message, PendingOp, PayoutLedger
from .payswift import PaySwift, idempotency_key
from .tz import ist_day

log = logging.getLogger("qd.agent")

MAX_LLM_CALLS_PER_MESSAGE = 2     # hard budget guard (review §3.2)
MESSAGE_POLL_SECONDS = 6.0        # duplicate waits for the in-flight copy at most this long

WINDOW_DAYS = 7


def _window_lo(received_day: date) -> date:
    return received_day - _dt.timedelta(days=WINDOW_DAYS - 1)


class Agent:
    def __init__(self, payswift: PaySwift | None = None):
        self.payswift = payswift or PaySwift(os.environ.get("PAYSWIFT_BASE_URL", "http://payswift:8081"))
        from . import llm  # optional layer; disabled without env keys
        self.llm = llm.get_llm()

    # ------------------------------------------------------------------ entry
    async def handle(self, rider_id: str, message_id: str, text: str, received_at: str) -> str:
        received_day = ist_day(received_at)
        rider_id = policy.norm_rider_id(rider_id)
        try:
            with session() as s:
                # FIRST writer wins: unique message_id doubles as the in-flight lock
                s.add(Message(message_id=message_id, rider_id=rider_id, text=text,
                              received_at=datetime.fromisoformat(received_at), status="processing"))
                s.commit()
        except IntegrityError:
            return await self._duplicate(message_id)
        return await self._process(rider_id, message_id, text, received_at, received_day)

    # ------------------------------------------------------------------ dedupe
    async def _duplicate(self, message_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + MESSAGE_POLL_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            with session() as s:
                msg = s.get(Message, message_id)
                if msg is not None and msg.status == "done":
                    return msg.reply or ""
            await asyncio.sleep(0.4)
        return "Aapka request process ho raha hai, thodi der mein reply aayega."

    # ------------------------------------------------------------------ pipeline
    async def _process(self, rider_id: str, message_id: str, text: str,
                       received_at: str, received_day: date) -> str:
        s = session()
        try:
            convo = s.execute(select(Conversation).where(Conversation.rider_id == rider_id)).scalar_one_or_none()
            if convo is None:
                convo = Conversation(rider_id=rider_id)
                s.add(convo)
                s.commit()
            conv_id = convo.id
            trace.record(s, rider_id, conv_id, "message_in", "rider_message",
                         {"message_id": message_id, "text": text, "received_at": received_at}, None)
            s.commit()

            it = intent.parse(text, received_at)
            # language continuity: a terse follow-up like "20 wala" has no language
            # markers — keep replying in the language the conversation opened with.
            prev_lang = _load_convo_lang(convo)
            if it.language == "hinglish":
                _store_convo_lang(convo, "hinglish")
            elif prev_lang == "hinglish":
                it.language = "hinglish"
            trace.record(s, rider_id, conv_id, "tool_call", "parse_intent", {"text": text}, _intent_out(it))
            s.commit()

            # ---- escalation-worthy fast paths -------------------------------
            if it.injection:
                return await self._finish_simple(s, rider_id, conv_id, it, "injection", message_id=message_id,
                                                 escalate_reason=f"prompt injection in message {message_id}")
            if it.impersonation:
                return await self._finish_simple(s, rider_id, conv_id, it, "impersonation", message_id=message_id,
                                                 escalate_reason=f"tried to act as {it.referenced_riders}")
            if it.vague and not it.claims:
                return await self._finish_simple(s, rider_id, conv_id, it, "ask_details", message_id=message_id)

            if it.pushback and not any(c.trip_ids for c in it.claims):
                return await self._pushback(s, rider_id, message_id, conv_id, it, convo)
            # pure follow-up: status request, or references an amount we already acted on
            if self._is_followup(s, convo, it, text):
                lastd = replies.state_last_decisions(convo)
                replies.state_update(convo, )
                reply = replies.followup_reply(it.language, lastd) or replies.ask_details(it.language)
                trace.record(s, rider_id, conv_id, "reply", "reply_followup", None, reply)
                return await self._finish(s, rider_id, message_id, reply)

            # ---- investigation (claim-scoped; never scan the rider's whole ledger)
            clusters: list[dict] = []
            trip_claims = [c for c in it.claims if c.trip_ids]

            if trip_claims:
                # All named trips in one message are ONE dispute (one cluster of items)
                kind = trip_claims[0].kind
                items: list[engine.Item] = []
                for c in trip_claims:
                    inv = engine.investigate_claim(s, rider_id, c)
                    trace.record(s, rider_id, conv_id, "tool_call", "investigate_trip",
                                 {"trip_ids": c.trip_ids, "kind": c.kind}, _inv_out(inv))
                    s.commit()
                    if inv.blocked == "not_yours":
                        return await self._finish_simple(s, rider_id, conv_id, it, "not_yours",
                                                         message_id=message_id,
                                                         escalate_reason="disputed a trip not owned by this account")
                    if inv.blocked == "trip_not_found":
                        return await self._finish_simple(s, rider_id, conv_id, it, "trip_not_found",
                                                         message_id=message_id)
                    items.extend(inv.items)
                outside = [i for i in items if i.day is not None and not (_window_lo(received_day) <= i.day <= received_day)]
                items = [i for i in items if i not in outside]
                if not items and outside:
                    return await self._finish_simple(s, rider_id, conv_id, it, "out_of_window",
                                                     message_id=message_id,
                                                     escalate_reason="dispute outside 7-day window",
                                                     facts={"date": str(outside[0].day)})
                if items:
                    clusters.append({"kind": kind, "day": min(i.day for i in items), "items": items,
                                     "trip_ids": [i.trip_id for i in items if i.trip_id]})
                else:
                    tid = trip_claims[0].trip_ids[0]
                    # reuse the engine's investigation facts verbatim (trip_status,
                    # cancelled_penalty note, surge, distance, expected/actual)
                    last_inv = engine.investigate_claim(s, rider_id, intent.Claim(kind=kind, day=None, trip_ids=[tid]))
                    inv_f: dict = {"kind": kind, **{k: v for k, v in last_inv.facts.__dict__.items()
                                                    if v is not None}}
                    return await self._finish_simple(s, rider_id, conv_id, it, "no_discrepancy",
                                                     message_id=message_id, facts=inv_f,
                                                     escalate=_should_escalate_no_issue(kind, inv_f))

            day_clusters = self._investigate_day_claims(s, rider_id, conv_id, it, received_day)
            clusters.extend(day_clusters)

            if not clusters:
                return await self._finish_simple(s, rider_id, conv_id, it, "ask_details", message_id=message_id)

            return await self._resolve_and_finish(s, rider_id, message_id, conv_id, it,
                                                  clusters, received_day)
        finally:
            s.close()

    # ------------------------------------------------------------------ day claims
    def _investigate_day_claims(self, s: Session, rider_id: str, conv_id: int,
                                it: intent.Intent, received_day: date) -> list[dict]:
        clusters = []
        for c in it.claims:
            if c.trip_ids:
                continue
            day = c.day
            if day is None:
                # topic without a date: sweep only self-scoped claims. Incentive claims
                # ("12 se zyada order kiye") name the fact, not the day — the window
                # sweep finds the newest eligible-but-unpaid day (R005 shape). Generic
                # payout complaints without a date are asked for details instead.
                if c.kind != "incentive":
                    continue
                for offset in range(0, WINDOW_DAYS):
                    d = received_day - _dt.timedelta(days=offset)
                    inv = engine.investigate_claim(s, rider_id, intent.Claim(kind=c.kind, day=d))
                    if inv.items:
                        day = d
                        break
                if day is None:
                    continue
            else:
                if not (_window_lo(received_day) <= day <= received_day):
                    clusters.append({"kind": c.kind, "day": day, "items": []})
                    continue
                inv = engine.investigate_claim(s, rider_id, c)
                trace.record(s, rider_id, conv_id, "tool_call", "investigate_day",
                             {"day": str(day), "kind": c.kind}, _inv_out(inv))
                s.commit()
            items = [i for i in inv.items if i.day is None or _window_lo(received_day) <= i.day <= received_day]
            clusters.append({"kind": c.kind, "day": day, "items": items})
        # keep only payable or explainable clusters
        return clusters

    def _is_followup(self, s: Session, convo: Conversation, it: intent.Intent, text: str) -> bool:
        if not it.followup:
            return False
        if any(c.trip_ids for c in it.claims):
            return False
        if any(c.day for c in it.claims if c.kind != "fare"):
            return False
        # amount references: "10 wala bhi de do na" — 10 matches a recent decision amount
        amounts = {int(n) for n in _NUM_RE.findall(text)}
        recent = {int(d.get("amount") or 0) for d in replies.state_last_decisions(convo)}
        if amounts & recent:
            return True
        return not amounts and not it.claims

    async def _pushback(self, s: Session, rider_id: str, message_id: str, conv_id: int,
                        it: intent.Intent, convo: Conversation) -> str:
        """Rider disagreed after our explanation → re-state facts + escalate (review §3.7).
        Conversation state carries 'last_inv'; the model doesn't have to remember."""
        st = replies.state_last_inv(convo)
        if not st:
            return await self._finish_simple(s, rider_id, conv_id, it, "ask_details",
                                             message_id=message_id)
        facts = st.get("facts") or {}
        lang = it.language
        t = replies.HING if lang == "hinglish" else replies.ENG
        summary = replies.pushback_summary(lang, facts)
        reply = replies._fmt(t["pushback_recheck"], facts=summary)
        replies.state_update(convo, pushback_delta=1)
        self._queue_escalation(s, rider_id, conv_id,
                               "rider insists after re-check; record shows no shortfall " +
                               json.dumps(facts, default=str))
        s.commit()
        trace.record(s, rider_id, conv_id, "reply", "reply_pushback", None, reply)
        s.commit()
        return await self._finish(s, rider_id, message_id, reply)

    # ------------------------------------------------------------------ resolution
    async def _resolve_and_finish(self, s: Session, rider_id: str, message_id: str,
                                  conv_id: int, it: intent.Intent, clusters: list,
                                  received_day: date) -> str:
        payable = []
        for c in clusters:
            c = dict(c)
            c["covered"] = self._drop_compensated(s, rider_id, c["items"])
            c["items"] = c["covered"]
            amount = sum(i.diff for i in c["items"])
            if amount > 0:
                payable.append(c)
        if not payable:
            # distinguish a genuine out-of-window complaint from a verified no-issue
            for c in clusters:
                if not c["items"] and c.get("day") and not (_window_lo(received_day) <= c["day"] <= received_day):
                    return await self._finish_simple(s, rider_id, conv_id, it, "out_of_window",
                                                     message_id=message_id,
                                                     escalate_reason="dispute outside 7-day window",
                                                     facts={"date": str(c["day"])})
            c0 = clusters[0]
            facts = self._facts_for_cluster(s, rider_id, c0)
            return await self._finish_simple(s, rider_id, conv_id, it, "no_discrepancy",
                                             message_id=message_id, facts=facts,
                                             escalate=_should_escalate_no_issue(c0["kind"], facts))

        # per-rider serialization of decide→write→pay (advisory lock on Postgres)
        dialect = s.bind.dialect.name
        if dialect == "postgresql":
            s.execute(text("select pg_advisory_xact_lock(:k)"),
                      {"k": int(hashlib.sha1(rider_id.encode()).hexdigest()[:12], 16)})
        # SQLite (tests/dev): single-writer semantics make the write txn the critical section

        # compensation check INSIDE the lock so concurrent messages can't both pay
        for c in payable:
            c["items"] = self._drop_compensated(s, rider_id, c["items"])
        payable = [c for c in payable if c["items"]]
        if not payable:
            s.rollback()
            c0 = clusters[0]
            return await self._finish_simple(s, rider_id, conv_id, it, "no_discrepancy",
                                             message_id=message_id,
                                             facts=self._facts_for_cluster(s, rider_id, c0))

        # Cross-cluster dedupe: the same discrepancy id can surface in TWO clusters
        # (rider names a trip and also claims its date), and each id must be paid
        # at most once. Keep every item only in its FIRST cluster.
        seen_ids: set[str] = set()
        for c in payable:
            fresh = [i for i in c["items"] if i.id not in seen_ids]
            seen_ids |= {i.id for i in fresh}
            c["items"] = fresh
        payable = [c for c in payable if c["items"]]

        decision = resolver.decide_clusters(s, rider_id, received_day, payable)
        trace.record(s, rider_id, conv_id, "decision", "resolve_dispute",
                     {"clusters": [{"kind": c["kind"], "day": str(c["day"]),
                                    "items": [i.id for i in c["items"]],
                                    "amount": sum(i.diff for i in c["items"])} for c in payable],
                      "auto_used_today": resolver.auto_payout_used_today(s, rider_id, received_day)},
                     {"actions": [{"type": a["type"], "amount": a["amount"],
                                   "items": [i.id for i in a["cluster"]["items"]]} for a in decision["actions"]]})
        s.commit()

        parts: list[str] = []
        new_decisions: list[dict] = []
        first = True
        for a in decision["actions"]:
            c = a["cluster"]
            # live re-check inside the loop: items paid by an earlier action in this
            # same message are now covered by committed ledger rows and stop here.
            items = self._drop_compensated(s, rider_id, c["items"])
            if not items:
                continue
            live_amount = sum(i.diff for i in items)   # never trust a stale amount
            details = ", ".join(replies.item_detail(it.language, i) for i in items)
            if first and details:
                parts.append(("Check ho gaya: " if it.language == "hinglish" else "Checked: ") + details + ".")
                first = False
            elif details:
                parts.append(details.capitalize() if not details[0].isdigit() else details + ".")
            if a["type"] == "auto_pay":
                out = await self._pay(s, rider_id, conv_id, live_amount, items, received_day)
                if out.ok:
                    parts.append(replies._fmt(replies.HING["okay_paid"] if it.language == "hinglish" else replies.ENG["okay_paid"],
                                              amount=live_amount,
                                              ids=", ".join(i.trip_id or str(i.day) for i in items)))
                    new_decisions.append({"type": "paid", "amount": live_amount,
                                          "trip_ids": [i.trip_id for i in items if i.trip_id]})
                else:
                    parts.append(replies.HING["error"] if it.language == "hinglish" else replies.ENG["error"])
            else:
                # drop the once-per-day hint line and the queue-approval amount
                op = await self._queue_approval(s, rider_id, conv_id, live_amount, items, received_day,
                                                reason=f"verified shortfall ₹{live_amount} "
                                                       f"({'second same-day dispute → once-per-day auto-pay rule' if live_amount <= resolver.AUTO_CAP else 'over ₹200 auto-pay limit'}); "
                                                       f"items: {', '.join(i.id for i in items)}")
                parts.append(replies._fmt(replies.HING["okay_approval"] if it.language == "hinglish" else replies.ENG["okay_approval"],
                                          amount=live_amount))
                new_decisions.append({"type": "approval", "amount": live_amount,
                                      "trip_ids": [i.trip_id for i in items if i.trip_id]})

        # record the last investigation snapshot for pushback handling
        c0 = decision["actions"][0]["cluster"] if decision["actions"] else clusters[0]
        snap = {"kind": c0["kind"], "day": str(c0["day"]),
                "facts": self._facts_for_cluster(s, rider_id, c0)}
        replies.state_last_inv_store(convo_of(s, rider_id), snap)
        replies.state_update(convo_of(s, rider_id), decisions=new_decisions)
        s.commit()
        reply = " ".join(p for p in parts if p).strip() or replies.ask_details(it.language)
        trace.record(s, rider_id, conv_id, "reply", "reply_resolution", None, reply)
        return await self._finish(s, rider_id, message_id, reply)

    # ------------------------------------------------------------------ facts
    def _facts_for_cluster(self, s: Session, rider_id: str, cluster: dict) -> dict:
        kind, day = cluster["kind"], cluster.get("day")
        facts: dict = {"kind": kind, "date": str(day) if day else ""}
        trips = repo_trips_day(s, rider_id, day) if day else []
        if kind == "incentive":
            facts["trips_done"] = len([t for t in trips if t.status == "completed"])
            inc = repo_incentive_lines(s, rider_id, day) if day else []
            facts["incentive_paid"] = int(inc[0].amount) if inc else None
        elif kind == "penalty":
            pls = repo_penalty_lines(s, rider_id, day) if day else []
            cancels = len([t for t in trips if t.status == "cancelled_by_rider"])
            facts["penalty_lines"], facts["rider_cancels"] = len(pls), cancels
            facts["note"] = "penalty_correct" if facade(pls, cancels) else None
        else:
            facts["trips_done"] = len(trips)
        if cluster.get("trip_ids"):
            t = s.execute(select(TripTR).where(TripTR.trip_id == cluster["trip_ids"][0])).scalars().first()
            if t is not None and t.norm_rider_id == rider_id:
                facts["trip_id"] = t.trip_id
                facts["expected"] = policy.trip_fare(t.distance_km, t.surge_multiplier)
                line = repo_payout_line(s, t.trip_id)
                facts["actual"] = int(line.amount) if line else 0
                facts["surge_multiplier"] = float(t.surge_multiplier)
        return facts

    # ------------------------------------------------------------------ helpers
    def _drop_compensated(self, s: Session, rider_id: str, items: list[engine.Item]) -> list[engine.Item]:
        rows = s.execute(select(PayoutLedger).where(
            PayoutLedger.rider_id == rider_id,
            PayoutLedger.status.in_(["held", "paid", "queued"]))).scalars().all()
        covered: set[str] = set()
        for r in rows:
            try:
                covered |= set(json.loads(r.covered_ids or "[]"))
            except ValueError:
                continue
        return [i for i in items if i.id not in covered]

    async def _pay(self, s: Session, rider_id: str, conv_id: int, amount: int,
                   items: list[engine.Item], day: date) -> _PayResult:
        covered = [i.id for i in items]
        key = idempotency_key(rider_id, day, covered)
        row = PayoutLedger(rider_id=rider_id, day=day, amount=amount,
                           covered_ids=json.dumps(covered), decided_by="auto",
                           status="held", idem_key=key)
        s.add(row)
        s.commit()
        ref = f"dispute {covered[0]} | {rider_id} | {day}"
        try:
            resp = await self.payswift.payout(rider_id, amount, ref, key)
        except Exception as e:  # user-facing safety over stack purity
            row.status = "failed"
            s.commit()
            trace.record(s, rider_id, conv_id, "error", "issue_payout",
                         {"amount": amount, "idem_key": key}, {"error": str(e)})
            s.commit()
            return _PayResult(ok=False)
        row.payswift_payout_id = resp.get("payout_id")
        row.status = "paid"
        s.commit()
        trace.record(s, rider_id, conv_id, "tool_call", "issue_payout",
                     {"amount": amount, "idem_key": key, "covered": covered}, resp)
        s.commit()
        return _PayResult(ok=True, payout_id=resp.get("payout_id"))

    async def _queue_approval(self, s: Session, rider_id: str, conv_id: int, amount: int,
                              items: list[engine.Item], day: date, reason: str) -> PendingOp:
        covered = [i.id for i in items]
        key = idempotency_key(rider_id, day, covered)
        ledger = PayoutLedger(rider_id=rider_id, day=day, amount=amount,
                              covered_ids=json.dumps(covered), decided_by="pending_ops",
                              status="queued", idem_key=key)
        s.add(ledger)
        op = PendingOp(rider_id=rider_id, conversation_id=conv_id, type="approval",
                       amount=amount, reason=reason, covered_ids=json.dumps(covered), idem_key=key)
        s.add(op)
        s.commit()
        ledger.pending_op_id = op.id
        s.commit()
        trace.record(s, rider_id, conv_id, "tool_call", "request_ops_approval",
                     {"amount": amount, "items": covered, "reason": reason}, {"pending_op_id": op.id})
        s.commit()
        return op

    def _queue_escalation(self, s: Session, rider_id: str, conv_id: int, reason: str) -> PendingOp:
        op = PendingOp(rider_id=rider_id, conversation_id=conv_id, type="escalation",
                       amount=None, reason=reason)
        s.add(op)
        s.commit()
        trace.record(s, rider_id, conv_id, "tool_call", "escalate_to_ops",
                     {"reason": reason}, {"pending_op_id": op.id})
        s.commit()
        return op

    async def _finish_simple(self, s: Session, rider_id: str, conv_id: int, it,
                             kind: str, *, message_id: str = "", facts=None,
                             escalate: bool = False, escalate_reason: str | None = None) -> str:
        lang = it.language
        t = replies.HING if lang == "hinglish" else replies.ENG
        if kind == "injection":
            reply, name = replies._fmt(t["injection"]), "reply_injection"
        elif kind == "impersonation":
            reply, name = replies._fmt(t["impersonation"]), "reply_impersonation"
        elif kind == "not_yours":
            reply, name = replies._fmt(t["not_yours"]), "reply_not_yours"
        elif kind == "trip_not_found":
            reply, name = replies._fmt(t["trip_not_found"]), "reply_trip_not_found"
        elif kind == "out_of_window":
            reply, name = replies._fmt(t["out_of_window"], date=(facts or {}).get("date", "")), "reply_out_of_window"
        elif kind == "no_discrepancy":
            reply, name = replies.no_discrepancy(lang, facts or {}), "reply_no_discrepancy"
        else:
            reply, name = replies.ask_details(lang), "reply_ask_details"
        trace.record(s, rider_id, conv_id, "reply", name, None, reply)
        if kind in ("injection", "impersonation", "not_yours", "out_of_window") or escalate:
            self._queue_escalation(s, rider_id, conv_id, escalate_reason or _default_escalation_reason(kind, facts or {}))
            s.commit()
        if kind in ("no_discrepancy", "ask_details"):
            replies.state_last_inv_store(convo_of(s, rider_id),
                                         {"kind": (facts or {}).get("kind"), "day": (facts or {}).get("date"),
                                          "facts": facts or {}})
        s.commit()
        return await self._finish(s, rider_id, message_id, reply)

    def _wrap_finish(self, s: Session, rider_id: str, message_id: str, conv_id: int, it,
                     reply: str, trace_name: str) -> str:
        return reply

    async def _finish(self, s: Session, rider_id: str, message_id: str, reply: str) -> str:
        # optional LLM polish (bounded; numbers verified unchanged by llm._validate)
        polished = self.llm.maybe_polish(reply) if self.llm.enabled() else None
        if polished:
            reply = polished
        with session() as s2:
            if message_id:
                msg = s2.get(Message, message_id)
                if msg is not None:
                    msg.status = "done"
                    msg.reply = reply
            s2.commit()
        return reply


class _PayResult:
    def __init__(self, ok: bool, payout_id: str | None = None):
        self.ok = ok
        self.payout_id = payout_id


_NUM_RE = None  # set at import below


def _set_re() -> None:  # pragma: no cover
    global _NUM_RE
    import re as _re
    _NUM_RE = _re.compile(r"(?<![\dT])(\d{1,4})(?![\d])")


_set_re()


# ---- local repo shims (keeps this file the single orchestrator) -----------------
def repo_trips_day(s, rider_id, day):
    from . import repo
    return repo.trips_for_day(s, rider_id, day) if day else []


def repo_incentive_lines(s, rider_id, day):
    from . import repo
    return repo.incentive_lines(s, rider_id, day) if day else []


def repo_penalty_lines(s, rider_id, day):
    from . import repo
    return repo.penalty_lines(s, rider_id, day) if day else []


def repo_payout_line(s, trip_id):
    from . import repo
    return repo.payout_line_for_trip(s, trip_id)


def facade(pls, cancels) -> bool:
    return len(pls) == cancels and cancels > 0


def convo_of(s, rider_id: str) -> Conversation:
    c = s.execute(select(Conversation).where(Conversation.rider_id == rider_id)).scalar_one_or_none()
    if c is None:
        c = Conversation(rider_id=rider_id)
        s.add(c)
        s.commit()
    return c


def _load_convo_lang(convo: Conversation) -> str | None:
    try:
        st = json.loads(convo.state or "{}")
        return st.get("language")
    except ValueError:
        return None


def _store_convo_lang(convo: Conversation, lang: str) -> None:
    st = json.loads(convo.state or "{}") if convo.state else {}
    st["language"] = lang
    convo.state = json.dumps(st)


def _default_escalation_reason(kind: str, facts: dict) -> str:
    if kind == "injection":
        return "prompt injection attempt"
    if kind == "impersonation":
        return "pretended to be another rider"
    if kind == "not_yours":
        return "dispute about a trip not owned by the caller"
    if kind == "out_of_window":
        return "dispute outside the 7-day window; flagged for human review"
    return "unclear or contestable dispute"


def _should_escalate_no_issue(kind: str, facts: dict) -> bool:
    if kind == "distance":              # "distance galat laga hai" (R036)
        return True
    if kind == "penalty" and (facts or {}).get("note") == "cancelled_penalty":  # R028
        return True
    if kind == "penalty" and not (facts or {}).get("items_ok", True):
        return True
    return False


def _inv_facts(claim, s, rider_id, trip_id, kind) -> dict:
    t = s.execute(select(TripTR).where(TripTR.trip_id == trip_id)).scalars().first()
    out: dict = {"kind": kind}
    if t is None:
        out["trip_not_found"] = True
        return out
    line = repo_payout_line(s, trip_id)
    out.update({"trip_id": t.trip_id, "trip_status": t.status,
                "surge_multiplier": float(t.surge_multiplier),
                "trip_distance": float(policy.clean_distance(t.distance_km)),
                "expected": policy.trip_fare(t.distance_km, t.surge_multiplier),
                "actual": int(line.amount) if line else 0,
                "date": str(t.ist_day)})
    return out


def _intent_out(it: intent.Intent) -> dict:
    return {"language": it.language,
            "claims": [{"kind": c.kind, "day": str(c.day) if c.day else None,
                        "trip_ids": c.trip_ids} for c in it.claims],
            "impersonation": it.impersonation, "injection": it.injection,
            "vague": it.vague, "followup": it.followup, "pushback": it.pushback}


def _inv_out(inv: engine.Investigation) -> dict:
    out: dict = {"items": [i.id for i in inv.items], "owed": inv.owed}
    if inv.blocked:
        out["blocked"] = inv.blocked
    for k in ("trips_done", "incentive_paid", "incentive_eligible", "penalty_lines",
              "rider_cancels", "surge_multiplier", "trip_distance", "trip_status",
              "note", "expected", "actual", "trip_id"):
        v = getattr(inv.facts, k)
        if v is not None:
            out[k] = v
    return out


# import inside functions to avoid a cycle w/ app.models typing helpers
from .models import Trip as TripTR  # noqa: E402

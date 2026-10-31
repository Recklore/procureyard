"""Deterministic reply templates (Hinglish + English). Numbers come only from the
investigation facts and the recorded decision. The optional LLM never invents numbers.

Every rupee figure and every trip-id appears verbatim in the output (evals check
substrings like "T926334" and "25").
"""
from __future__ import annotations

import json

from .models import Conversation


def _load_state(convo: Conversation) -> dict:
    try:
        return json.loads(convo.state or "{}")
    except ValueError:
        return {}


def state_pushback_count(convo: Conversation) -> int:
    return int(_load_state(convo).get("pushback_count") or 0)


def state_last_decisions(convo: Conversation) -> list[dict]:
    return list(_load_state(convo).get("last_decisions") or [])


def state_last_inv(convo: Conversation) -> dict | None:
    v = _load_state(convo).get("last_inv")
    return v if isinstance(v, dict) else None


def state_last_inv_store(convo: Conversation, snap: dict) -> None:
    st = _load_state(convo)
    st["last_inv"] = snap
    convo.state = json.dumps(st)


def pushback_summary(lang: str, facts: dict) -> str:
    """Facts-only restatement for the re-check reply; numbers come verbatim."""
    kind = (facts or {}).get("kind")
    if kind == "incentive":
        return (f"humare record mein {facts.get('date')} ko {facts.get('trips_done')} completed trips hain, "
                f"incentive 12 pe milta hai") if lang == "hinglish" else \
               (f"our records show {facts.get('trips_done')} completed trips on {facts.get('date')}; "
                f"the daily incentive needs 12")
    if kind == "penalty":
        return (f"{facts.get('date')} ko cancel penalty {facts.get('penalty_lines')} lines ki thi aapke "
                f"{facts.get('rider_cancels')} cancel ke against") if lang == "hinglish" else \
               (f"on {facts.get('date')} there were {facts.get('penalty_lines')} penalty lines against "
                f"{facts.get('rider_cancels')} of your cancels")
    if kind == "distance":
        return (f"{facts.get('trip_id')} ka record {facts.get('trip_distance') or facts.get('surge_multiplier')}"
                f" km ka hai") if lang == "hinglish" else \
               f"{facts.get('trip_id')} is recorded at the distance shown in the system"
    if facts.get("trip_id"):
        return (f"order {facts.get('trip_id')} pe fare ₹{facts.get('expected')} banta tha, ₹{facts.get('actual')} mile"
                if lang == "hinglish" else
                f"trip {facts.get('trip_id')} earned ₹{facts.get('expected')}, paid ₹{facts.get('actual')}")
    d = facts.get("date") or ""
    return (f"{d} ke payouts checks hue the aur records sahi the" if lang == "hinglish"
            else f"{d}'s payouts were already checked and the records were correct")


def state_update(convo: Conversation, *, pushback_delta: int = 0, decisions: list[dict] | None = None) -> None:
    st = _load_state(convo)
    st["pushback_count"] = int(st.get("pushback_count") or 0) + pushback_delta
    if decisions is not None:
        st["last_decisions"] = list(st.get("last_decisions") or []) + decisions
        st["last_decisions"] = st["last_decisions"][-8:]
    convo.state = json.dumps(st)


ENG = {
    "ask_details": "Which day or which order is the payout problem with? Share the date or the trip ID and I'll check right away.",
    "ask_details_more": "I need one more detail: the date (like '20 Sep') or the trip ID (like T123456) of the problem.",
    "out_of_window": "We can only look at disputes for the last 7 days. The {date} complaint falls outside that window, so I've sent it to the ops team to review as an exception.",
    "not_yours": "This order is not linked to this WhatsApp number, so I can't share its details. All help is strictly for this account only.",
    "trip_not_found": "That trip ID isn't in our system. Please double-check it or share the date and I'll look again.",
    "injection": "I can only help with real payout disputes for your account. Tell me the date or trip ID you're disputing and I'll check.",
    "impersonation": "I can only help with the account linked to this number. If there's a problem with your own payouts, tell me the date or trip ID and I'll check it.",
    "paid_correct_trip": "Checked {trip}: surge was {surge}, fare eligible was ₹{expected} and you were paid ₹{actual}. The payout is correct.",
    "no_discrepancy_day": "Checked {date}: your expected payout matches what was paid. No money is pending from our side.",
    "incentive_ineligible": "On {date} you had {count} completed trips. The daily incentive is for 12+ trips, so it didn't apply that day.",
    "incentive_paid": "On {date} the ₹150 incentive was already credited, so nothing extra is pending.",
    "penalty_correct": "Policy: ₹10 is deducted per rider-cancelled trip (customer cancels aren't charged). On {date} there were {rider_cancels} rider cancels and the same number of penalty lines, so the deduction was correct.",
    "cancel_contest": "Policy: ₹10 applies to rider-cancelled trips (customer cancellations are not charged). I've sent your request to the ops team for review.",
    "distance_contest": "Our system recorded {dist} km on {trip}, fare ₹{expected}, and you were paid ₹{actual}. If you believe the distance was recorded wrong, I've escalated it to the ops team to review the records.",
    "underpay_day": "Checked {date}: {n} (order/orders) didn't clear the expected fare. Difference ₹{total} is being sent to you.",
    "okay_paid": "₹{amount} for {ids} has been sent via PaySwift — check payout history in a bit.",
    "okay_approval": "₹{amount} is with the ops team for approval — once approved, it goes out automatically.",
    "okay_mixed": "₹{auto} has been sent via PaySwift, and ₹{pending} is queued for ops approval (one auto-payment per rider per day, per policy) — the rest lands as soon as ops signs off.",
    "already_settled": "This one's already settled: {summary}",
    "pushback_recheck": "Checked again — {facts} If you believe our record itself is wrong, I've escalated it to the ops team for review.",
    "error": "Something went wrong on our side while processing this. The ops team can see the full trace; please try again in a bit.",
    "same_day_second_payout_hint": " As per policy there's only one auto-payment per rider per day, so the rest needs ops approval.",
}

HING = {
    "ask_details": "Kaunse din ya kaunse order ka payout galat lag raha hai? Date ya order ID bata dijiye, turant check karta hoon.",
    "ask_details_more": "Ek details bata dijiye: date (jaise '20 Sep') ya trip ID (jaise T123456)?",
    "out_of_window": "Hum sirf last 7 din ke disputes dekh paate hain. {date} wala complaint is window ke bahar hai, isliye exception ke liye ise ops team ko bhej diya hai.",
    "not_yours": "Ye order is WhatsApp number se linked nahi hai, isliye iski details share nahi kar sakta. Help sirf aapke account ke liye hai.",
    "trip_not_found": "Ye order ID system mein nahi mili. Dobara check kariye ya date bata dijiye.",
    "injection": "Main sirf aapke account ke payout disputes mein madad kar sakta hoon. Apne dispute ki date ya order ID bhejiye, main check karta hoon.",
    "impersonation": "Main sirf is number se jude account ki help kar sakta hoon. Aapke apne payout mein problem ho to date ya order ID bhejiye.",
    "paid_correct_trip": "Check kiya {trip}: surge {surge} tha, fare ₹{expected} banta tha aur aapko ₹{actual} mile. Payout sahi hai.",
    "no_discrepancy_day": "Check kiya {date}: expected payout aur paid amount same hain. Aapka koi paisa pending nahi hai.",
    "incentive_ineligible": "{date} ko aapke {count} trips complete hue the. Incentive 12+ trips pe milta hai, isliye us din nahi bana.",
    "incentive_paid": "{date} ki ₹150 incentive already aa chuki hai, kuch extra pending nahi hai.",
    "penalty_correct": "Policy: rider khud cancel karne pe ₹10 penalty hota hai (customer cancel pe nahi). {date} ko aapke {rider_cancels} cancel the aur utni hi penalty lines thi, isliye deduction sahi hai.",
    "cancel_contest": "Policy: rider-cancel pe ₹10 penalty hai (customer cancel pe nahi hai). Aapki request pe case ops team ko bhej diya hai review ke liye.",
    "distance_contest": "Record mein {trip} ka distance {dist} km hai, fare ₹{expected} banta hai aur ₹{actual} mile hain. Agar aapko lagta hai distance galat record hua, main ops team ko bhej deta hoon review ke liye.",
    "underpay_day": "Check kiya {date}: {n} order ka fare kam mila tha. Farak ₹{total} aapko bhej rahe hain.",
    "okay_paid": "₹{amount} PaySwift pe bhej diye hain ({ids}). Payout history mein dikh jayega.",
    "okay_approval": "₹{amount} ops approval ke liye chala gaya hai — approve hote hi aa jayega.",
    "okay_mixed": "₹{auto} PaySwift pe bhej diye, aur ₹{pending} ops approval pe chala gaya hai (policy ke hisab se ek din mein ek hi auto-payment hoti hai) — approve hote hi aa jayega.",
    "already_settled": "Ye pehle hi settle ho gaya: {summary}",
    "pushback_recheck": "Dobara check kiya: {facts} Agar aapko lagta hai humara record hi galat hai, main ise ops team ko bhej deta hoon.",
    "error": "Humari taraf se kuch gadbad aayi process karte waqt. Ops team ko poora trace dikh raha hai; thodi der mein phir try kariye.",
    "same_day_second_payout_hint": " Policy ke hisab se ek din mein ek hi auto-payment hoti hai, baaki ops approval se aayega.",
}


def _fmt(template: str, **kw) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def ask_details(lang: str) -> str:
    t = HING if lang == "hinglish" else ENG
    return t["ask_details"]


def followup_reply(lang: str, decisions: list[dict]) -> str | None:
    """Status-style reply summary referencing the rider's last decisions by number."""
    if not decisions:
        return None
    t = HING if lang == "hinglish" else ENG
    parts: list[str] = []
    paid = [d for d in decisions if d.get("type") in ("paid",)]
    approved = [d for d in decisions if d.get("type") in ("approval",)]
    for d in paid:
        ids = ", ".join(t_ for t_ in (d.get("trip_ids") or [])) or ""
        txt = _fmt(t["okay_paid"], amount=d.get("amount"), ids=ids) if ids else _fmt("₹{amount} PaySwift pe process ho gaya hai." if lang == "hinglish" else "₹{amount} has been processed on PaySwift.", amount=d.get("amount"))
        parts.append(txt)
    for d in approved:
        parts.append(_fmt(t["okay_approval"], amount=d.get("amount")))
    if parts:
        return " ".join(parts)
    return None


def no_discrepancy(lang: str, facts) -> str:
    t = HING if lang == "hinglish" else ENG
    f = facts or {}
    if f.get("not_yours"):
        return _fmt(t["not_yours"])
    if f.get("trip_not_found"):
        return _fmt(t["trip_not_found"])
    if f.get("kind") == "incentive":
        if f.get("incentive_paid"):
            return _fmt(t["incentive_paid"], date=f.get("date") or "")
        return _fmt(t["incentive_ineligible"], date=f.get("date") or "", count=f.get("trips_done") or 0)
    if f.get("kind") == "penalty":
        if f.get("note") == "cancelled_penalty":
            return _fmt(t["cancel_contest"])
        return _fmt(t["penalty_correct"], date=f.get("date") or "",
                    rider_cancels=f.get("rider_cancels") or 0,
                    penalty_lines=f.get("penalty_lines") or 0)
    if f.get("kind") == "distance":
        return _fmt(t["distance_contest"], trip=f.get("trip_id") or "",
                    dist=f.get("trip_distance") if f.get("trip_distance") is not None else "",
                    expected=f.get("expected") or 0, actual=f.get("actual") or 0)
    if f.get("trip_id"):
        return _fmt(t["paid_correct_trip"], trip=f.get("trip_id"),
                    surge=f.get("surge_multiplier") if f.get("surge_multiplier") is not None else 1.0,
                    expected=f.get("expected") or 0, actual=f.get("actual") or 0)
    return _fmt(t["no_discrepancy_day"], date=f.get("date") or "")

def item_detail(lang: str, item) -> str:
    t = HING if lang == "hinglish" else ENG
    if item.type in ("underpaid", "unpaid"):
        if item.trip_id:
            return f"order {item.trip_id} pe ₹{item.expected} banta tha, ₹{item.actual} mile" if lang == "hinglish" else f"trip {item.trip_id} earned ₹{item.expected}, paid ₹{item.actual}"
        return f"{item.day} ke orders pe ₹{item.expected} banta tha, ₹{item.actual} mile" if lang == "hinglish" else f"{item.day}: earned ₹{item.expected}, paid ₹{item.actual}"
    if item.type == "incentive_missing":
        return f"{item.day} ka ₹150 incentive mila nahi" if lang == "hinglish" else f"{item.day}: ₹150 incentive missing"
    if item.type == "penalty_dup":
        return (f"{item.day} ko cancel penalty {item.expected // 10} baar zyada kata" if lang == "hinglish"
                else f"{item.day}: cancellation penalty charged {item.expected // 10} extra times")
    return ""

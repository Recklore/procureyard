"""Intent parsing — deterministic, no LLM required. Understands the Hinglish shapes
riders use in data/conversations.json plus generic English variants. The optional LLM
layer (app/llm.py) can rephrase/assist, but the parser alone must handle everything.

Design: segment the text on connectives ("aur", comma, period) and pair each segment's
own date marker with its own complaint topic, so "20 ko surge mila ... 21 ko penalty"
yields two properly-scoped claims instead of a wrong cross-product.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .tz import to_ist

_TRIP_RE = re.compile(r"(?<![\w])T(\d{5,7})(?![\w])")
_RIDER_RE = re.compile(r"(?<![\w])[Rr]\s?0?(\d{1,3})(?![\w])")

INJECTION_MARKERS = (
    "system:", "system message", "ignore all previous", "ignore the above",
    "new instructions", "developer mode", "jailbreak", "approve karo turant",
    "approve all", "sab disputes",
)
FOLLOWUP_RE = re.compile(
    r"\bkab tak\b|\bde do\b|\bdedo\b|\babhi de\b|\bdobara check\b|\bre[- ]?check\b|\bcheck karo\b|"
    r"\bthik hai\b|\btheek hai\b|\bok\b|\bruko\b|\bprocess\b|\baaya\?*|\bmila\?*|\bwaiting\b|"
    r"\bstill\b|\bstatus\b|\bkab aayega\b")
PUSHBACK_RE = re.compile(
    r"\bdobara check\b|\bre[- ]?check\b|\baur dekh\b|\bphir check\b|\bnahi nahi\b|\bwrong\b|"
    r"\bgalat hai\b|\bhr baar\b|\bah bhai\b|\bsun\b.*\bcheck\b")

_SEGMENT_SPLIT = re.compile(r"\baur\b|\bbhi\b|,|\.|;|!|\band\b|\bphir\b", re.IGNORECASE)

INCENTIVE_HINTS = (r"incentiv", re.compile(r"\b12\s*(?:se zyada|se badhkar|order|orders|trips?|rides?)\s*(kiye|complete|kiye)?\b"),
                   r"\b12\+")
PENALTY_HINTS = (r"penalt", r"\bcancel\b.*\bkata", r"kata do baar", r"double risk", r"do baar kata", r"duplicate")


@dataclass
class Claim:
    kind: str                      # fare|surge|distance|missing_payment|incentive|penalty
    day: date | None
    trip_ids: list[str] = field(default_factory=list)


@dataclass
class Intent:
    language: str                  # hinglish | english
    claims: list[Claim] = field(default_factory=list)
    impersonation: bool = False
    injection: bool = False
    referenced_riders: list[str] = field(default_factory=list)
    vague: bool = False            # complaint without any date/trip/topic anchor
    followup: bool = False         # status request / continuation ("kab tak aayega?")
    pushback: bool = False         # "dobara check karo" style rider disagreement
    raw: str = ""


_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
           "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
           "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}

_MONTH_WORDS = r"(sept|sep|september|january|february|march|april|may|june|july|august|october|november|december|jan|feb|mar|apr|jun|jul|aug|oct|nov|dec)"

# "18 tarikh ko | 20 wala | 19 sept | sep 19 | 21st | 17th"
_DAY_MARKS = re.compile(
    r"(?:\b(\d{1,2})\s*(?:tarikh|tareekh|wala|wali|th|st|nd|rd)\s*(?:ko|ka|ke|ka|of)?\b)"
    r"|(?:\b(\d{1,2})\s*ko\b)"
    r"|(?:\b(?:tarikh\w*|wala)\s*(\d{1,2})\b)"
    r"|(?:\b" + _MONTH_WORDS + r"\s*(\d{1,2})\b)"
    r"|(?:\b(\d{1,2})\s*" + _MONTH_WORDS + r"\b)", re.IGNORECASE)

_MONTH_DATE_ALTS = 4 | 8  # alternation indices (1-based) holding month words


def _day_from_match(m: re.Match, received: datetime) -> date | None:
    """Group map (see _DAY_MARKS): 0|1|2 = bare day-numbers; 3=month, 4=day for
    'sep 19'; 5=day, 6=month for '19 sept'."""
    today = received.date()
    groups = m.groups()
    if groups[0] is not None:
        d = int(groups[0]); mon = today.month
    elif groups[1] is not None:
        d = int(groups[1]); mon = today.month
    elif groups[2] is not None:
        d = int(groups[2]); mon = today.month
    elif groups[3] is not None:
        mon = _MONTHS.get(groups[3].lower(), today.month); d = int(groups[4])
    elif groups[5] is not None:
        d = int(groups[5]); mon = _MONTHS.get(groups[6].lower(), today.month)
    else:
        return None
    if not (1 <= d <= 31):
        return None
    try:
        return date(today.year, mon, d)
    except ValueError:
        return None


def extract_days(text: str, received: datetime) -> list[date]:
    out: list[date] = []
    for m in _DAY_MARKS.finditer(text):
        d = _day_from_match(m, received)
        if d is not None:
            out.append(d)
    low = text.lower()
    if re.search(r"\bkal ka\b|\bkal ko\b|\bkal\b|\byesterday\b", low):
        out.append(received.date() - timedelta(days=1))
    if re.search(r"\baaj ka\b|\baaj ko\b|\baaj\b|\btoday\b", low):
        out.append(received.date())
    return sorted(set(out))


def kinds_in(text: str) -> list[str]:
    low = text.lower()
    kinds: list[str] = []
    if "surge" in low:
        kinds.append("surge")
    if "incentiv" in low or re.search(r"\b12\s*(?:se zyada|se badhkar|order|orders|trip|trips|ride|rides|kiye|complete|kiye|hua|hue)\b", low) or "12+" in low:
        kinds.append("incentive")
    if re.search(r"penalt?|cancel\b|kata (do baar|dobara|double)|do baar kata|2 baar kata|double kata", low) and re.search(r"penal|cancel|kata|kata hai", low):
        kinds.append("penalty")
    if re.search(r"distance|km\b|kilomet|galti se|door ki|fare (gram|riya)", low):
        kinds.append("distance")
    if re.search(r"payment nahi aaya|payment nahi mila|paisa nahi aaya|paisa nahi mila|paisa hi nahi|"
                 r"payment missing|paisa missing|payment nahi[^,]{0,20}aaya|orders? ka paisa", low):
        kinds.append("missing_payment")
    if re.search(r"payout (galat|kam|wrong|sahi nahi)|kam aaya|kam aaye|ekdm kam|bilkul kam", low):
        kinds.append("fare")
    if not kinds and ("payment" in low or "paisa" in low or "payout" in low or "rupee" in low or "rupay" in low):
        kinds.append("fare")
    # order matters for explanations, keep stable, drop dups
    seen, uniq = set(), []
    for k in kinds:
        if k not in seen:
            seen.add(k); uniq.append(k)
    return uniq


def parse(text: str, received_at: str) -> Intent:
    received_ist = to_ist(datetime.fromisoformat(received_at))
    low = text.lower().strip()

    trip_ids = [f"T{m.group(1)}" for m in _TRIP_RE.finditer(text)]
    referenced_riders = [f"R{int(m.group(1)):03d}" for m in _RIDER_RE.finditer(text)]
    injection = any(marker in low for marker in INJECTION_MARKERS)
    # caller identity arrives from the phone number (out-of-band). Any rider-id typed
    # inside the message is an attempt to act as (or inject) some account.
    impersonation = bool(referenced_riders)

    followup = bool(FOLLOWUP_RE.search(low)) and not trip_ids
    pushback = bool(PUSHBACK_RE.search(low))

    has_dates = bool(extract_days(low, received_ist))
    vague = not trip_ids and not has_dates and not kinds_in(low) and not followup

    claims: list[Claim] = []
    if trip_ids:
        # named trips: only the_narrowest topic they're attached to
        seg_kinds = kinds_in(low)
        # segment-wise pairing: attach kind of segment containing each trip
        segs = [seg for seg in _SEGMENT_SPLIT.split(low) if seg.strip()]
        for tid in trip_ids:
            tkind = seg_kinds[0] if seg_kinds else "fare"
            for seg in segs:
                if f"t{tid[1:].lower()}" in seg.lower() or tid.lower() in seg.lower():
                    k = kinds_in(seg)
                    if k:
                        tkind = k[0]
            claims.append(Claim(kind=tkind, day=None, trip_ids=[tid]))
            # a date printed near the trip gets bound into the same claim
            for m in _DAY_MARKS.finditer(low):
                d = _day_from_match(m, received_ist)
                if d is not None and claims:
                    claims[0].day = d  # if one, share the trip's date context
    else:
        # segment-augmented pairing
        segs = [seg for seg in _SEGMENT_SPLIT.split(low) if seg.strip()]
        paired: list[tuple[date | None, list[str]]] = []
        for seg in segs:
            ks = kinds_in(seg)
            ds = extract_days(seg, received_ist)
            if not ks:
                continue
            if ds:
                for d in ds:
                    paired.append((d, ks))
            else:
                paired.append((None, ks))
        if not paired:  # kinds/dates present but segment split lost them
            ks, ds = kinds_in(low), extract_days(low, received_ist)
            for d in (ds or [None]):
                paired.append((d, ks))
        for d, ks in paired:
            seen_local = set()
            for k in ks:
                if (k, d) in seen_local:
                    continue
                seen_local.add((k, d))
                claims.append(Claim(kind=k, day=d))
        # cross-pair any stray dates lacking topics
        if not claims and has_dates:
            for d in extract_days(low, received_ist):
                claims.append(Claim(kind="fare", day=d))

    return Intent(language="hinglish" if re.search(r"\b(bhai|yaar|nahi|mila|kata|kiye|karo|kaata|thik|banta|de do|ruro)\b", low) else "english",
                  claims=claims, impersonation=impersonation, injection=injection,
                  referenced_riders=referenced_riders, vague=vague, followup=followup,
                  pushback=pushback, raw=text)

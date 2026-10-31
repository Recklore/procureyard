# Rider Payout Dispute Desk — QuickDrop

AI assistant that resolves rider WhatsApp payout disputes end-to-end: reads the
dispute, investigates the trips/payout ledger with deterministic code, fixes what's
actually owed (≤₹200 auto-pay once per rider per day; anything larger or later goes
to ops), explains what isn't, and escalates anything shady or contestable.

## Run it

```bash
docker compose up --build
# wait for GET /health → 200, then:
python evals/run.py http://localhost:8000
# ops dashboard: http://localhost:8000/ops   (conversations, pending queue, traces)
```

Everything runs with **no LLM key** (deterministic templates are the canonical voice).
If `LLM_API_KEY` is set, replies are optionally rephrased — the rewriter is gated so
it can never change a rupee figure, trip ID, or date (`app/llm.py`).

```
service :8000   •   Postgres :5432   •   PaySwift sandbox :8081
```

## Design — what only code decides, what the LLM may do

| Layer | Who | What |
|---|---|---|
| Intent | code (`app/intent.py`) | parse Hinglish/English claims: dates (incl. "kal"), trip IDs, topics; detect impersonation & injection |
| Money | code (`app/policy.py`, `app/engine.py`) | fare (₹25 + ₹6/km after 2 km, surge on the whole fare, `ROUND_HALF_UP`), ₹150 incentive at 12+ distinct completed trips, ₹10 rider-cancel penalties, IST day, 7-day window anchored on the message's `received_at` |
| Amounts | code (`app/resolver.py`) | verified shortfall per dispute; ≤₹200 → auto-pay (once per rider per IST day); else → ops approval for the FULL amount (never a partial ₹200) |
| Paying | code (`app/payswift.py`, ledger) | deterministic `Idempotency-Key` (`rider|day|covered-ids`), ledger row written before the call, retries only on idempotent-safe codes |
| Wording | LLM (optional) | rephrase the approved reply; every number/ID preserved verbatim |
| Rider ID | code | bound out-of-band from the message envelope; no tool accepts a rider_id |

Data-quality fixes baked in (Phase 0, `scripts/analyze_data.py`):
- corrupt distances (`5000` = 5.0 km — payout ledger proves the true value);
- typo'd rider ids in trips.csv (`R7`→`R007`, 64 rows);
- **distinct**-trip counting for incentive eligibility (trips.csv has 10 duplicated rows);
- disputes scoped only to what the rider named (a date or trip IDs) — the agent never
  scans-and-pays the rider's whole ledger.

Safety invariants (enforced by code, asserted by tests):
- never pays more than the verified shortfall; overpayments are never "refunded";
- covered discrepancy IDs live in `payout_ledger`/`pending_ops` → the same trip,
  incentive or penalty can't be compensated twice, across conversations or re-sends;
- re-sent `message_id` polls the in-flight copy instead of re-running the agent;
- per-rider advisory lock serializes decide→write→pay (Postgres).

## Evals

`python evals/run.py <service-url>` → per-scenario PASS/FAIL (24 scenarios:
payout totals via PaySwift, approval amounts, escalation rules, reply mentions,
duplicate delivery, impersonation, injection, out-of-window refusal). Verified
locally: 24/24 PASS, plus unit/scenario tests (`pytest tests -q`, 27 passed).

## Assumptions

- Solver decisions and reply wording follow `data/conversations.json` expected
  outcomes; escalation and pushback behavior mirrors the finance note.
- "Last 7 days" includes today (anchored on `received_at`).
- `payout_lines.amount` is signed (penalties are −10).

## Layout

`app/` service+agent · `evals/` runner · `scripts/analyze_data.py` Phase-0 scan ·
`tests/` unit+scenario tests · `templates/ops.html` dashboard.

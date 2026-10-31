"""PaySwift sandbox client — deterministic wrapper around POST/GET /v1/payouts.

Every payout call carries a deterministic Idempotency-Key (rider + day + covered
discrepancy ids), so a timeout-then-retry, or a second approve click, cannot pay twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os

import httpx

log = logging.getLogger("qd.payswift")

RETRY_STATUS = {429, 500, 502, 503, 504}


class PaySwiftError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"payswift {status}: {body[:200]}")
        self.status = status
        self.body = body


def idempotency_key(rider_id: str, day, covered_ids: list[str]) -> str:
    payload = json.dumps({"r": rider_id, "d": str(day), "ids": sorted(covered_ids)}, sort_keys=True)
    return f"qd-{rider_id}-{day}-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


class PaySwift:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("PAYSWIFT_BASE_URL", "http://payswift:8081")).rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=12.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def payout(self, rider_id: str, amount: int, reference: str, idem_key: str) -> dict:
        """POST /v1/payouts with a deterministic Idempotency-Key. Retries only on the
        idempotent-safe statuses; a 409 means this exact payout was already accepted."""
        body = {"rider_id": rider_id, "amount": amount, "reference": reference}
        last = None
        for attempt in range(4):
            try:
                r = await self._client.post(
                    "/v1/payouts", json=body,
                    headers={"Idempotency-Key": idem_key})
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code == 409:
                    # same key already used — treat as already issued
                    try:
                        existing = await self.list_payouts(rider_id)
                        return {"payout_id": (existing[-1].get("payout_id") if existing else None),
                                "status": "paid", "duplicate": True}
                    except Exception:
                        return {"payout_id": None, "status": "paid", "duplicate": True}
                if r.status_code in RETRY_STATUS:
                    last = PaySwiftError(r.status_code, r.text)
                    await asyncio.sleep(1.0 * (attempt + 1))  # 1,2,3s backoff
                    continue
                raise PaySwiftError(r.status_code, r.text)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last = e
                await asyncio.sleep(1.0 * (attempt + 1))
        raise last if last is not None else PaySwiftError(0, "unknown")

    async def list_payouts(self, rider_id: str | None = None) -> list[dict]:
        params = {"rider_id": rider_id} if rider_id else {}
        r = await self._client.get("/v1/payouts", params=params)
        if r.status_code != 200:
            raise PaySwiftError(r.status_code, r.text)
        body = r.json()
        return body if isinstance(body, list) else body.get("payouts", [])

    async def health(self) -> bool:
        try:
            r = await self._client.get("/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

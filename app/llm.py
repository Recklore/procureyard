"""Optional LLM layer. With no API key configured everything below is a no-op and the
deterministic template replies (already containing all verified numbers) go to the rider.

Two bounded uses (≤2 calls per message; review §3.2 / §4.4):
  - maybe_polish: rephrase the final template reply more naturally, keeping every
    rupee figure and trip id VERBATIM.
Providers: Gemini (generativelanguage) and Groq (OpenAI-compatible). Timeout-short;
any failure falls back to the template — the eval never sees a 500.
"""
from __future__ import annotations

import os

import httpx

MAX_CALLS_PER_MESSAGE = 2

_SHORT_TIMEOUT = httpx.Timeout(6.0, connect=3.0)

SYSTEM = (
    "You rewrite payout-dispute replies for QuickDrop's WhatsApp bot. Output ONLY the "
    "rewritten user-visible reply, same language style and length as input, warm and clear. "
    "Never change, drop, or add any rupee amounts, trip IDs, dates, or counts. Never promise "
    "anything beyond what the input says. No emojis."
)


class LLM:
    def __init__(self):
        self.key = os.environ.get("LLM_API_KEY", "").strip()
        self.model = os.environ.get("LLM_MODEL", "gemini-2.0-flash").strip()
        self.provider = os.environ.get("LLM_PROVIDER", "").strip()
        if not self.provider:
            self.provider = "gemini" if ("google" in self.model or "gemini" in self.model) else "groq"
        self.base_url = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
        self._calls_left = MAX_CALLS_PER_MESSAGE
        self._client = httpx.Client(timeout=_SHORT_TIMEOUT)

    def enabled(self) -> bool:
        return bool(self.key)

    def maybe_polish(self, reply: str) -> str | None:
        if not self.enabled() or self._calls_left <= 0:
            return None
        self._calls_left -= 1
        try:
            if self.provider == "gemini":
                return self._gemini(reply)
            return self._openai_compat(reply)
        except Exception:  # noqa: BLE001 — fallback must never break the pipeline
            return None

    def _gemini(self, reply: str) -> str | None:
        url = (self.base_url or "https://generativelanguage.googleapis.com") + \
              f"/v1beta/models/{self.model}:generateContent?key={self.key}"
        r = self._client.post(url, json={
            "system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"parts": [{"text": reply}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 300},
        })
        if r.status_code != 200:
            return None
        body = r.json()
        parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return _validate(reply, text)

    def _openai_compat(self, reply: str) -> str | None:
        url = (self.base_url or "https://api.groq.com/openai/v1") + "/chat/completions"
        r = self._client.post(url, headers={"Authorization": f"Bearer {self.key}"}, json={
            "model": self.model,
            "temperature": 0.3,
            "max_tokens": 300,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": reply}],
        })
        if r.status_code != 200:
            return None
        text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return _validate(reply, text)


def _validate(original: str, polished: str | None) -> str | None:
    """Only accept a polish that preserved every number and trip id from the original."""
    if not polished or len(polished) > 800:
        return None
    import re

    def toks(t: str) -> set[str]:
        return set(re.findall(r"T\d{5,7}|\d+", t))

    if toks(original) - toks(polished):
        return None
    return polished


_SINGLETON: LLM | None = None


def get_llm() -> LLM:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = LLM()
    return _SINGLETON

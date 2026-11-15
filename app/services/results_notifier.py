"""Tell Katie (AI call agent) that a case's results are ready.

    POST {RESULTS_WEBHOOK_URL}
    X-Signature: sha256=<hex HMAC-SHA256 of the raw body, key RESULTS_WEBHOOK_SECRET>
    {"event": "income.ready" | "expenditure.ready", "owner_ref": "...",
     "status": "done" | "failed", "at": "<ISO time>"}

The notice carries NO figures and no client data beyond the reference: Katie
fetches the results with its own API key (GET /v1/case-results?owner_ref=).
Off when RESULTS_WEBHOOK_URL is empty. Never raises: a missed notice must not
fail a run, and Katie can always read the results itself.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

INCOME_READY = "income.ready"
EXPENDITURE_READY = "expenditure.ready"


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def notify(event: str, owner_ref: str, status: str, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> bool:
    url = (settings.results_webhook_url or "").strip()
    if not url:
        return False
    body = json.dumps({"event": event, "owner_ref": owner_ref, "status": status,
                       "at": datetime.now(timezone.utc).isoformat()}).encode()
    headers = {"Content-Type": "application/json"}
    if settings.results_webhook_secret:
        headers["X-Signature"] = sign(body, settings.results_webhook_secret)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10, transport=transport) as client:
                r = await client.post(url, content=body, headers=headers)
            if r.status_code < 300:
                return True
            logger.warning("results notice %s for %s: HTTP %s", event, owner_ref, r.status_code)
            if r.status_code < 500:
                return False
        except httpx.HTTPError as exc:
            logger.warning("results notice %s for %s failed: %s", event, owner_ref, exc)
        await asyncio.sleep(2 ** attempt)
    return False

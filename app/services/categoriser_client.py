"""HTTP client for LedgerFlow v2's service API (/v1/statement-jobs).

Pure: no database. `categorise()` submits a case's statements, polls the job
and fetches the ledger, and reports through callbacks, so the submit / poll /
resubmit rules can be tested with a mock transport (tests/test_categoriser.py).
The database side lives in categorisation_service.py.

LedgerFlow contract (ledgerflow/services/integration/routes.py):
    POST /v1/statement-jobs            files[], account_holder_name, external_ref
                                       -> 202 {job_id, ...}
    GET  /v1/statement-jobs/{id}       -> {status: QUEUED|PROCESSING|CATEGORIZING
                                                  |REVIEW_READY|FAILED, error, ...}
    GET  /v1/statement-jobs/{id}/ledger -> the categorised ledger
All with `Authorization: Bearer <key>`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

READY, FAILED = "REVIEW_READY", "FAILED"
RUNNING = ("QUEUED", "PROCESSING", "CATEGORIZING")

#: How many times a transient HTTP failure (timeout, 5xx, connection) is
#: retried on one call before the run is failed.
TRANSIENT_RETRIES = 3


class CategoriserError(Exception):
    """A failure the run should record. `retryable` = worth a later retry."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class JobLost(Exception):
    """LedgerFlow no longer has the job (404, or failed by its restart)."""


@dataclass
class Statement:
    file_name: str
    content: bytes
    mime_type: str


@dataclass
class Outcome:
    ok: bool
    job_id: str | None = None
    ledger: dict[str, Any] | None = None
    error: str | None = None
    file_errors: list[Any] = field(default_factory=list)
    match_rate: float | None = None
    summary: dict[str, Any] | None = None


@dataclass
class Config:
    base_url: str
    api_key: str
    poll_seconds: float = 5.0
    job_timeout_seconds: float = 1800.0
    request_timeout_seconds: float = 120.0
    max_resubmits: int = 2


def _lost_by_restart(error: str | None) -> bool:
    # LedgerFlow's own wording for a job cut off by its restart
    # (catjobs/jobs.py RESTARTED): the statement was fine, send it again.
    return bool(error) and "restarted" in error.lower()


async def _call(client: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
    """One request, retrying transient failures; raises CategoriserError."""
    last: str = ""
    for attempt in range(TRANSIENT_RETRIES):
        try:
            reply = await client.request(method, url, **kw)
        except httpx.TransportError as exc:
            last = f"LedgerFlow unreachable: {exc.__class__.__name__}: {exc}"
        else:
            if reply.status_code < 500:
                return reply
            last = f"LedgerFlow error {reply.status_code}: {reply.text[:300]}"
        await asyncio.sleep(min(2 ** attempt, 10))
    raise CategoriserError(last, retryable=True)


def _refused(reply: httpx.Response, what: str) -> CategoriserError:
    if reply.status_code in (401, 403):
        return CategoriserError(f"{what}: LedgerFlow refused the service key "
                                f"({reply.status_code}). Check CATEGORISER_API_KEY.")
    if reply.status_code == 503:
        return CategoriserError(f"{what}: LedgerFlow has no SERVICE_API_KEYS configured.")
    try:
        detail = reply.json()
    except ValueError:
        detail = reply.text[:300]
    return CategoriserError(f"{what}: LedgerFlow answered {reply.status_code}: {detail}")


async def _submit(client, statements, holder, ref) -> str:
    files = [("files", (s.file_name, s.content, s.mime_type)) for s in statements]
    data = {"account_holder_name": holder or "", "external_ref": ref}
    reply = await _call(client, "POST", "/v1/statement-jobs", files=files, data=data)
    if reply.status_code not in (200, 202):
        raise _refused(reply, "Submitting statements")
    return reply.json()["job_id"]


async def _wait(client, job_id: str, cfg: Config,
                sleep: Callable[[float], Awaitable[None]]) -> dict[str, Any]:
    deadline = time.monotonic() + cfg.job_timeout_seconds
    while True:
        reply = await _call(client, "GET", f"/v1/statement-jobs/{job_id}")
        if reply.status_code == 404:
            raise JobLost(f"job {job_id} not found")
        if reply.status_code != 200:
            raise _refused(reply, "Checking the job")
        job = reply.json()
        status = (job.get("status") or "").upper()
        if status == READY:
            return job
        if status == FAILED:
            if _lost_by_restart(job.get("error")):
                raise JobLost(job.get("error"))
            return job
        if time.monotonic() > deadline:
            raise CategoriserError(
                f"LedgerFlow job {job_id} did not finish within "
                f"{int(cfg.job_timeout_seconds)}s (last status {status or 'unknown'}).",
                retryable=True)
        await sleep(cfg.poll_seconds)


async def categorise(
    statements: list[Statement],
    *,
    holder: str | None,
    ref: str,
    cfg: Config,
    on_submitted: Callable[[str], Awaitable[None]] | None = None,
    existing_job_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Outcome:
    """Send, wait, fetch. Never raises for a LedgerFlow-side problem: the
    Outcome says what happened. `existing_job_id` resumes polling a job that
    was already submitted (after this service restarted)."""
    if not cfg.base_url or not cfg.api_key:
        return Outcome(ok=False, error="Categoriser is enabled but CATEGORISER_URL / "
                                       "CATEGORISER_API_KEY is not set.")
    if not statements and not existing_job_id:
        return Outcome(ok=False, error="No bank statements on this case.")

    async with httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        timeout=cfg.request_timeout_seconds,
        transport=transport,
    ) as client:
        job_id = existing_job_id
        resubmits = 0
        try:
            while True:
                if job_id is None:
                    job_id = await _submit(client, statements, holder, ref)
                    if on_submitted is not None:
                        await on_submitted(job_id)
                try:
                    job = await _wait(client, job_id, cfg, sleep)
                except JobLost as lost:
                    if resubmits >= cfg.max_resubmits or not statements:
                        return Outcome(ok=False, job_id=job_id,
                                       error=f"LedgerFlow lost the job: {lost}")
                    logger.warning("LedgerFlow lost job %s (%s); resubmitting", job_id, lost)
                    resubmits += 1
                    job_id = None
                    continue
                break

            file_errors = job.get("file_errors") or []
            if (job.get("status") or "").upper() == FAILED:
                return Outcome(ok=False, job_id=job_id, file_errors=file_errors,
                               error=job.get("error") or "LedgerFlow failed the job.")

            reply = await _call(client, "GET", f"/v1/statement-jobs/{job_id}/ledger")
            if reply.status_code != 200:
                raise _refused(reply, "Fetching the ledger")
            ledger = reply.json()
            # The same placement as figures with proof (group -> label ->
            # transactions, latest month, category -> subcategory). Optional:
            # a LedgerFlow without the route still gives the ledger.
            summary = None
            try:
                reply = await _call(client, "GET", f"/v1/statement-jobs/{job_id}/summary")
                if reply.status_code == 200:
                    summary = reply.json()
                else:
                    logger.info("LedgerFlow job %s: no summary (%s)", job_id, reply.status_code)
            except CategoriserError as exc:
                logger.warning("LedgerFlow job %s: summary unavailable: %s", job_id, exc)
            return Outcome(ok=True, job_id=job_id, ledger=ledger, summary=summary,
                           file_errors=file_errors, match_rate=job.get("match_rate"))
        except CategoriserError as exc:
            return Outcome(ok=False, job_id=job_id, error=str(exc))

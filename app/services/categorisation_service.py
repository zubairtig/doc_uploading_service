"""Send a case's bank statements to LedgerFlow and keep the categorised result.

Flow (one `categorisation_run` row per attempt, see the model):

  upload of a bank_statement (API or client link)
    -> queue_for_case(): record a PENDING run, start a background task
    -> wait CATEGORISER_DEBOUNCE_SECONDS; if a newer run exists for the case,
       this one is SUPERSEDED (the newer one will carry this file too)
    -> claim it (PENDING -> PROCESSING, atomically), read EVERY bank statement
       on the case from storage, send them to LedgerFlow as one job
    -> poll; store the ledger (DONE) or the reason (FAILED)

Original file bytes are sent, never the OCR text: LedgerFlow reads the PDF's
columns itself, and the column positions are what say money in vs money out.

Restart-safe: runs live in Postgres, and resume_unfinished() at start-up picks
up PENDING runs and re-polls PROCESSING ones by their LedgerFlow job id.
Background tasks are in-process: run ONE app process (as docker-compose does),
or move this onto the Celery worker before scaling out.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.categorisation_run import CategorisationRun, CategorisationStatus
from app.models.document import Document
from app.services import categoriser_client, results_notifier
from app.services.storage import get_storage_backend

logger = logging.getLogger(__name__)

#: Keeps background tasks referenced until they finish (asyncio only holds
#: weak references to tasks).
_TASKS: set[asyncio.Task] = set()


def enabled() -> bool:
    return bool(settings.categoriser_enabled)


def wants(doc_type: str | None) -> bool:
    """Should a document of this type trigger categorisation of its case?"""
    return enabled() and (doc_type or "") in settings.categoriser_document_types


def _config() -> categoriser_client.Config:
    return categoriser_client.Config(
        base_url=settings.categoriser_url,
        api_key=settings.categoriser_api_key,
        poll_seconds=settings.categoriser_poll_seconds,
        job_timeout_seconds=settings.categoriser_job_timeout_seconds,
        request_timeout_seconds=settings.categoriser_request_timeout_seconds,
        max_resubmits=settings.categoriser_max_resubmits,
    )


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# ── queueing ─────────────────────────────────────────────────────────────────

async def queue_for_case(
    *, tenant_id: uuid.UUID, owner_ref: str, client_name: str | None,
    debounce: bool = True,
) -> uuid.UUID | None:
    """Record a PENDING run for the case and start it in the background.

    Call AFTER the upload is committed. Never raises: a categorisation
    problem must not fail the client's upload.
    """
    if not enabled():
        return None
    try:
        async with AsyncSessionLocal() as db:
            run = CategorisationRun(
                tenant_id=tenant_id, owner_ref=owner_ref,
                client_name=(client_name or None), status=CategorisationStatus.PENDING,
            )
            db.add(run)
            await db.commit()
            run_id = run.id
        _spawn(_run(run_id, debounce=debounce))
        logger.info("categorisation queued for case %s (run %s)", owner_ref, run_id)
        return run_id
    except Exception:  # noqa: BLE001 — never fail the upload
        logger.exception("could not queue categorisation for case %s", owner_ref)
        return None


async def resume_unfinished() -> None:
    """At start-up: carry on every run a restart interrupted."""
    if not enabled():
        return
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(CategorisationRun.id, CategorisationRun.status,
                   CategorisationRun.ledgerflow_job_id)
            .where(CategorisationRun.status.in_(CategorisationStatus.UNFINISHED))
        )).all()
    for run_id, status, job_id in runs:
        if status == CategorisationStatus.PROCESSING and job_id:
            _spawn(_resume(run_id, job_id))
        elif status == CategorisationStatus.PROCESSING:
            # Claimed but never submitted: send it again from the start.
            async with AsyncSessionLocal() as db:
                await db.execute(update(CategorisationRun)
                                 .where(CategorisationRun.id == run_id)
                                 .values(status=CategorisationStatus.PENDING))
                await db.commit()
            _spawn(_run(run_id, debounce=False))
        else:
            _spawn(_run(run_id, debounce=True))
    if runs:
        logger.info("resumed %d unfinished categorisation run(s)", len(runs))


# ── the work ─────────────────────────────────────────────────────────────────

async def _statements_for(db: AsyncSession, run: CategorisationRun):
    """Every categorisable document on the case, read from storage."""
    docs = (await db.execute(
        select(Document)
        .where(Document.tenant_id == run.tenant_id,
               Document.owner_ref == run.owner_ref,
               Document.document_type.in_(settings.categoriser_document_types))
        .order_by(Document.created_at)
    )).scalars().all()

    backend = get_storage_backend("local")
    statements, sent, missing = [], [], []
    for doc in docs:
        url = doc.s3_url or ""
        if "/files/" not in url:
            missing.append({"file": doc.file_name, "error": "not in local storage"})
            continue
        try:
            content = await asyncio.to_thread(backend.load, url.split("/files/", 1)[-1])
        except OSError as exc:
            missing.append({"file": doc.file_name, "error": f"could not read: {exc}"})
            continue
        statements.append(categoriser_client.Statement(doc.file_name, content, doc.mime_type))
        sent.append({"id": str(doc.id), "file_name": doc.file_name})
    return statements, sent, missing


async def _superseded(db: AsyncSession, run: CategorisationRun) -> bool:
    newer = (await db.execute(
        select(CategorisationRun.id)
        .where(CategorisationRun.tenant_id == run.tenant_id,
               CategorisationRun.owner_ref == run.owner_ref,
               CategorisationRun.status == CategorisationStatus.PENDING,
               CategorisationRun.created_at > run.created_at)
        .limit(1)
    )).scalar_one_or_none()
    return newer is not None


async def _run(run_id: uuid.UUID, *, debounce: bool) -> None:
    try:
        if debounce and settings.categoriser_debounce_seconds > 0:
            await asyncio.sleep(settings.categoriser_debounce_seconds)

        async with AsyncSessionLocal() as db:
            run = await db.get(CategorisationRun, run_id)
            if run is None or run.status != CategorisationStatus.PENDING:
                return
            if await _superseded(db, run):
                run.status = CategorisationStatus.SUPERSEDED
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()
                return
            # Claim atomically: a second task for the same run does nothing.
            claimed = (await db.execute(
                update(CategorisationRun)
                .where(CategorisationRun.id == run_id,
                       CategorisationRun.status == CategorisationStatus.PENDING)
                .values(status=CategorisationStatus.PROCESSING,
                        attempts=CategorisationRun.attempts + 1)
                .returning(CategorisationRun.id)
            )).scalar_one_or_none()
            if claimed is None:
                return
            statements, sent, missing = await _statements_for(db, run)
            run.documents = sent
            await db.commit()
            holder, ref = run.client_name, run.owner_ref

        outcome = await categoriser_client.categorise(
            statements, holder=holder, ref=ref, cfg=_config(),
            on_submitted=_job_id_saver(run_id))
        await _finish(run_id, outcome, extra_file_errors=missing)
    except Exception as exc:  # noqa: BLE001 — a run must end FAILED, never stuck
        logger.exception("categorisation run %s crashed", run_id)
        await _finish(run_id, categoriser_client.Outcome(ok=False, error=str(exc)[:500]))


async def _resume(run_id: uuid.UUID, job_id: str) -> None:
    """Re-poll a job submitted before a restart (re-sending if LedgerFlow lost it)."""
    try:
        async with AsyncSessionLocal() as db:
            run = await db.get(CategorisationRun, run_id)
            if run is None:
                return
            statements, _, missing = await _statements_for(db, run)
            holder, ref = run.client_name, run.owner_ref
        outcome = await categoriser_client.categorise(
            statements, holder=holder, ref=ref, cfg=_config(),
            on_submitted=_job_id_saver(run_id), existing_job_id=job_id)
        await _finish(run_id, outcome, extra_file_errors=missing)
    except Exception as exc:  # noqa: BLE001
        logger.exception("categorisation run %s crashed on resume", run_id)
        await _finish(run_id, categoriser_client.Outcome(ok=False, error=str(exc)[:500]))


def _job_id_saver(run_id: uuid.UUID):
    async def save(job_id: str) -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(update(CategorisationRun)
                             .where(CategorisationRun.id == run_id)
                             .values(ledgerflow_job_id=job_id))
            await db.commit()
    return save


async def _finish(run_id: uuid.UUID, outcome: categoriser_client.Outcome,
                  extra_file_errors: list | None = None) -> None:
    async with AsyncSessionLocal() as db:
        run = await db.get(CategorisationRun, run_id)
        if run is None:
            return
        run.status = CategorisationStatus.DONE if outcome.ok else CategorisationStatus.FAILED
        run.ledgerflow_job_id = outcome.job_id or run.ledgerflow_job_id
        run.result = outcome.ledger
        run.summary = outcome.summary
        run.error = None if outcome.ok else (outcome.error or "unknown error")[:2000]
        run.file_errors = list(extra_file_errors or []) + list(outcome.file_errors or [])
        run.match_rate = outcome.match_rate
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
    logger.info("categorisation run %s: %s%s", run_id, run.status,
                f" ({run.error})" if run.error else "")
    await results_notifier.notify(results_notifier.EXPENDITURE_READY, run.owner_ref, run.status)


# ── reading ──────────────────────────────────────────────────────────────────

async def runs_for_case(db: AsyncSession, *, tenant_id: uuid.UUID, owner_ref: str,
                        limit: int = 20) -> list[CategorisationRun]:
    return list((await db.execute(
        select(CategorisationRun)
        .where(CategorisationRun.tenant_id == tenant_id,
               CategorisationRun.owner_ref == owner_ref)
        .order_by(CategorisationRun.created_at.desc())
        .limit(limit)
    )).scalars().all())

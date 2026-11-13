"""Send a case's income documents (and bank statements) to CAT, have CAT read
them, and keep the income CAT works out.

Flow:

  upload of a document whose type is in CAT_DOCUMENT_TYPE_MAP (API or link)
    -> queue_document(): a PENDING cat_document for the file + a PENDING cat_run
       for the case, then a background task
    -> wait CAT_DEBOUNCE_SECONDS; a newer PENDING run for the case supersedes
       this one (it will carry this file too)
    -> claim the run; sign in to CAT; upload every PENDING document of the case
       (POST proof/upload/); ask CAT to read the case (proof/recalculate/,
       waiting while CAT is busy with it); poll the case until CAT has read our
       files; fetch the evidence ledger (proof/) and keep its "Proof of Income"
       rows -> run DONE; tell Katie (results_notifier).

The CAT conversation itself is `push_and_read()`: pure (a CatClient in, an
outcome out), tested with a fake CAT in tests/test_cat_client.py.

Nothing here changes how this service treats an upload: it runs only when
CAT_ENABLED=true, after the upload is committed, and never raises into it.
Restart-safe: PENDING runs restart at start-up, and a PROCESSING run starts
over without re-uploading what CAT already has (those documents are SENT).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.cat_sync import CatDocument, CatDocumentStatus, CatRun, CatRunStatus
from app.models.document import Document
from app.services import cat_client, results_notifier
from app.services.storage import get_storage_backend
from app.services.storage.keys import slugify

logger = logging.getLogger(__name__)

_TASKS: set[asyncio.Task] = set()


def enabled() -> bool:
    return bool(settings.cat_enabled)


def cat_type_for(doc_type: str | None) -> str | None:
    """CAT's document_type for one of ours, or None when it is not sent to CAT."""
    doc_type = doc_type or ""
    if doc_type == "bank_statement" and not settings.cat_send_bank_statements:
        return None
    return settings.cat_document_type_map.get(doc_type)


def wants(doc_type: str | None) -> bool:
    return enabled() and cat_type_for(doc_type) is not None


def _config() -> cat_client.Config:
    return cat_client.Config(
        base_url=settings.cat_url, api_key=settings.cat_api_key,
        email=settings.cat_email, password=settings.cat_password,
        request_timeout_seconds=settings.cat_request_timeout_seconds)


def cat_name_for(owner_ref: str, doc_type: str, document_id: uuid.UUID, file_name: str) -> str:
    """A name unique on the case, so the file can be found again in CAT."""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "bin"
    return f"{slugify(owner_ref)}_{doc_type}_{document_id.hex}.{slugify(ext, 'bin')}"


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# ── the CAT conversation (pure) ──────────────────────────────────────────────

@dataclass
class Outgoing:
    key: str                      # our cat_document id, as text
    cat_name: str
    cat_document_type: str
    content: bytes | None
    mime_type: str
    notes: str = ""
    load_error: str | None = None


@dataclass
class Timing:
    poll_seconds: float = 15.0
    read_timeout_seconds: float = 900.0
    busy_retry_seconds: float = 30.0


@dataclass
class PassOutcome:
    ok: bool = False
    error: str | None = None
    # key -> {"ok": bool, "error": str|None, "reply": dict}
    uploads: dict[str, dict] = field(default_factory=dict)
    # cat_name -> CAT's reading of that file
    readings: dict[str, dict] = field(default_factory=dict)
    read_state: str = "not_needed"        # not_needed | done | timeout | failed
    income: list[dict] = field(default_factory=list)
    evidence_ledger: list[dict] = field(default_factory=list)


async def push_and_read(
    cat: cat_client.CatClient, cat_case: str, outgoing: list[Outgoing],
    awaiting: list[str], *, force_read: bool, timing: Timing,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PassOutcome:
    """Upload `outgoing`, have CAT read the case, return what it found.

    `awaiting`: cat_names CAT already has but had not read last time.
    `force_read`: read the case even with nothing new (a manual refresh).
    """
    out = PassOutcome()
    signed_in = await cat.sign_in()
    if not signed_in.ok:
        out.error = signed_in.error
        return out

    to_read = list(awaiting)
    for item in outgoing:
        if item.content is None:
            out.uploads[item.key] = {"ok": False, "error": item.load_error or "file missing"}
            continue
        up = await cat.upload(cat_case, item.cat_name, item.content, item.mime_type,
                              item.cat_document_type, item.notes)
        out.uploads[item.key] = {"ok": up.ok, "error": up.error, "reply": up.data}
        if up.ok:
            to_read.append(item.cat_name)

    if not to_read and not force_read:
        failures = [u["error"] for u in out.uploads.values() if not u["ok"]]
        out.ok = not failures
        out.error = "; ".join(failures)[:2000] or None
        return out

    # Ask CAT to read the case. One read at a time per case: while CAT is busy
    # (409) the running read may predate our upload, so ask again after it.
    deadline = time.monotonic() + timing.read_timeout_seconds
    while True:
        r = await cat.recalculate(cat_case)
        if not r.ok:
            out.read_state, out.error = "failed", r.error
            return out
        if r.data.get("state") == "started":
            break
        if time.monotonic() > deadline:
            out.read_state = "timeout"
            out.error = "CAT was busy reading this case the whole time."
            return out
        await sleep(timing.busy_retry_seconds)

    pending = set(to_read)
    while pending and time.monotonic() < deadline:
        await sleep(timing.poll_seconds)
        app = await cat.application(cat_case)
        if not app.ok:
            continue
        for name in list(pending):
            reading = cat_client.document_reading(cat_client.find_document(app.data, name))
            out.readings[name] = reading
            if reading["state"] in cat_client.READ_DONE + cat_client.READ_FAILED:
                pending.discard(name)

    proof = await cat.proof(cat_case)
    if proof.ok:
        out.income = cat_client.income_summary(proof.data)
        out.evidence_ledger = list(proof.data.get("evidence_ledger") or [])
    if pending:
        out.read_state = "timeout"
        out.error = (f"CAT had not finished reading {len(pending)} document(s) after "
                     f"{int(timing.read_timeout_seconds // 60)} minutes.")
    else:
        out.read_state = "done"
    if not proof.ok:
        out.error = "; ".join(filter(None, [out.error, proof.error]))
    out.ok = out.read_state == "done" and proof.ok
    return out


# ── queueing ─────────────────────────────────────────────────────────────────

async def queue_document(*, tenant_id: uuid.UUID, document: Document) -> uuid.UUID | None:
    """Record the document for CAT and start a pass over its case.

    Call AFTER the upload is committed. Never raises."""
    cat_type = cat_type_for(document.document_type)
    if not enabled() or cat_type is None:
        return None
    try:
        cat_case = cat_client.case_for(document.owner_ref, settings.cat_case_map)
        async with AsyncSessionLocal() as db:
            existing = (await db.execute(select(CatDocument.id).where(
                CatDocument.document_id == document.id))).scalar_one_or_none()
            if existing is None:
                db.add(CatDocument(
                    tenant_id=tenant_id, document_id=document.id,
                    owner_ref=document.owner_ref, cat_case=cat_case,
                    document_type=document.document_type, cat_document_type=cat_type,
                    cat_name=cat_name_for(document.owner_ref, document.document_type,
                                          document.id, document.file_name),
                    status=CatDocumentStatus.PENDING))
            run = CatRun(tenant_id=tenant_id, owner_ref=document.owner_ref,
                         cat_case=cat_case, trigger="upload", status=CatRunStatus.PENDING)
            db.add(run)
            await db.commit()
            run_id = run.id
        _spawn(_run(run_id, debounce=True))
        logger.info("CAT: %s queued for case %s (run %s)", document.document_type,
                    document.owner_ref, run_id)
        return run_id
    except Exception:  # noqa: BLE001 — never fail the upload
        logger.exception("CAT: could not queue document %s", getattr(document, "id", "?"))
        return None


async def queue_refresh(*, tenant_id: uuid.UUID, owner_ref: str) -> uuid.UUID | None:
    """POST /v1/cat: re-send what CAT refused, and read the case again."""
    if not enabled():
        return None
    cat_case = cat_client.case_for(owner_ref, settings.cat_case_map)
    async with AsyncSessionLocal() as db:
        await db.execute(update(CatDocument)
                         .where(CatDocument.tenant_id == tenant_id,
                                CatDocument.owner_ref == owner_ref,
                                CatDocument.status == CatDocumentStatus.FAILED)
                         .values(status=CatDocumentStatus.PENDING, error=None))
        run = CatRun(tenant_id=tenant_id, owner_ref=owner_ref, cat_case=cat_case,
                     trigger="manual", status=CatRunStatus.PENDING)
        db.add(run)
        await db.commit()
        run_id = run.id
    _spawn(_run(run_id, debounce=False))
    return run_id


async def resume_unfinished() -> None:
    """At start-up: carry on every pass a restart interrupted."""
    if not enabled():
        return
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(select(CatRun.id, CatRun.status).where(
            CatRun.status.in_(CatRunStatus.UNFINISHED)))).all()
        await db.execute(update(CatRun)
                         .where(CatRun.status == CatRunStatus.PROCESSING)
                         .values(status=CatRunStatus.PENDING))
        await db.commit()
    for run_id, status in runs:
        _spawn(_run(run_id, debounce=status == CatRunStatus.PENDING))
    if runs:
        logger.info("CAT: resumed %d unfinished pass(es)", len(runs))


# ── the work ─────────────────────────────────────────────────────────────────

async def _load(db: AsyncSession, cd: CatDocument) -> Outgoing:
    doc = await db.get(Document, cd.document_id)
    item = Outgoing(key=str(cd.id), cat_name=cd.cat_name,
                    cat_document_type=cd.cat_document_type, content=None,
                    mime_type=(doc.mime_type if doc else "") or "application/octet-stream",
                    notes=f"Uploaded by the client through the Document Collection Portal "
                          f"({cd.document_type}).")
    url = (doc.s3_url if doc else "") or ""
    if "/files/" not in url:
        item.load_error = "not in local storage"
        return item
    try:
        item.content = await asyncio.to_thread(
            get_storage_backend("local").load, url.split("/files/", 1)[-1])
    except OSError as exc:
        item.load_error = f"could not read: {exc}"
    return item


async def _superseded(db: AsyncSession, run: CatRun) -> bool:
    newer = (await db.execute(
        select(CatRun.id).where(CatRun.tenant_id == run.tenant_id,
                                CatRun.owner_ref == run.owner_ref,
                                CatRun.status == CatRunStatus.PENDING,
                                CatRun.created_at > run.created_at).limit(1)
    )).scalar_one_or_none()
    return newer is not None


async def _run(run_id: uuid.UUID, *, debounce: bool) -> None:
    try:
        if debounce and settings.cat_debounce_seconds > 0:
            await asyncio.sleep(settings.cat_debounce_seconds)
        async with AsyncSessionLocal() as db:
            run = await db.get(CatRun, run_id)
            if run is None or run.status != CatRunStatus.PENDING:
                return
            if await _superseded(db, run):
                run.status = CatRunStatus.SUPERSEDED
                run.finished_at = datetime.now(timezone.utc)
                await db.commit()
                return
            claimed = (await db.execute(
                update(CatRun).where(CatRun.id == run_id,
                                     CatRun.status == CatRunStatus.PENDING)
                .values(status=CatRunStatus.PROCESSING, attempts=CatRun.attempts + 1)
                .returning(CatRun.id))).scalar_one_or_none()
            await db.commit()
            if claimed is None:
                return
            docs = (await db.execute(select(CatDocument).where(
                CatDocument.tenant_id == run.tenant_id,
                CatDocument.owner_ref == run.owner_ref))).scalars().all()
            outgoing = [await _load(db, d) for d in docs if d.status == CatDocumentStatus.PENDING]
            awaiting = [d.cat_name for d in docs
                        if d.status in (CatDocumentStatus.SENT, CatDocumentStatus.READ_TIMEOUT)]
            cat_case, trigger, owner_ref = run.cat_case, run.trigger, run.owner_ref

        timing = Timing(settings.cat_poll_seconds, settings.cat_read_timeout_seconds,
                        settings.cat_busy_retry_seconds)
        async with cat_client.CatClient(_config()) as cat:
            outcome = await push_and_read(cat, cat_case, outgoing, awaiting,
                                          force_read=trigger == "manual", timing=timing)
        await _finish(run_id, outcome)
        await results_notifier.notify(results_notifier.INCOME_READY, owner_ref,
                                      CatRunStatus.DONE if outcome.ok else CatRunStatus.FAILED)
    except Exception as exc:  # noqa: BLE001 — a run must end FAILED, never stuck
        logger.exception("CAT: pass %s crashed", run_id)
        await _finish(run_id, PassOutcome(error=str(exc)[:500]))


async def _finish(run_id: uuid.UUID, outcome: PassOutcome) -> None:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        run = await db.get(CatRun, run_id)
        if run is None:
            return
        docs = (await db.execute(select(CatDocument).where(
            CatDocument.tenant_id == run.tenant_id,
            CatDocument.owner_ref == run.owner_ref))).scalars().all()
        for d in docs:
            up = outcome.uploads.get(str(d.id))
            if up is not None:
                d.attempts += 1
                if up["ok"]:
                    d.status, d.error, d.sent_at = CatDocumentStatus.SENT, None, now
                    d.cat_reply = up.get("reply") or {}
                else:
                    d.status, d.error = CatDocumentStatus.FAILED, (up["error"] or "")[:2000]
            reading = outcome.readings.get(d.cat_name)
            if reading is not None and d.status in (CatDocumentStatus.SENT,
                                                    CatDocumentStatus.READ_TIMEOUT):
                d.reading = reading
                if reading["state"] in cat_client.READ_DONE:
                    d.status = CatDocumentStatus.PROCESSED
                elif reading["state"] in cat_client.READ_FAILED:
                    d.status, d.error = CatDocumentStatus.READ_FAILED, "CAT could not read it."
                elif outcome.read_state == "timeout":
                    d.status = CatDocumentStatus.READ_TIMEOUT
        run.status = CatRunStatus.DONE if outcome.ok else CatRunStatus.FAILED
        run.error = None if outcome.ok else (outcome.error or "unknown error")[:2000]
        if outcome.income or outcome.evidence_ledger:
            run.result = {"income": outcome.income, "evidence_ledger": outcome.evidence_ledger,
                          "read_state": outcome.read_state}
        run.finished_at = now
        await db.commit()
    logger.info("CAT: pass %s %s%s", run_id, run.status,
                f" ({run.error})" if run.error else "")


# ── reading ──────────────────────────────────────────────────────────────────

async def case_state(db: AsyncSession, *, tenant_id: uuid.UUID, owner_ref: str) -> dict:
    """The case's CAT side: its documents, the newest pass, the newest income."""
    docs = (await db.execute(select(CatDocument, Document.file_name)
                             .join(Document, Document.id == CatDocument.document_id)
                             .where(CatDocument.tenant_id == tenant_id,
                                    CatDocument.owner_ref == owner_ref)
                             .order_by(CatDocument.created_at))).all()
    runs = (await db.execute(select(CatRun).where(
        CatRun.tenant_id == tenant_id, CatRun.owner_ref == owner_ref)
        .order_by(CatRun.created_at.desc()).limit(20))).scalars().all()
    visible = [r for r in runs if r.status != CatRunStatus.SUPERSEDED]
    latest = visible[0] if visible else None
    with_result = next((r for r in runs if r.result), None)
    return {
        "enabled": enabled(),
        "cat_case": (latest or with_result).cat_case if (latest or with_result) else
        cat_client.case_for(owner_ref, settings.cat_case_map),
        "status": latest.status if latest else None,
        "error": latest.error if latest else None,
        "finished_at": (with_result.finished_at.isoformat()
                        if with_result and with_result.finished_at else None),
        "documents": [{"document_id": str(d.document_id), "file_name": name,
                       "document_type": d.document_type,
                       "cat_document_type": d.cat_document_type, "status": d.status,
                       "reading": d.reading, "error": d.error,
                       "sent_at": d.sent_at.isoformat() if d.sent_at else None}
                      for d, name in docs],
        "income": (with_result.result or {}).get("income") if with_result else None,
        "evidence_ledger": (with_result.result or {}).get("evidence_ledger")
        if with_result else None,
    }

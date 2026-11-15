"""Bank-statement categorisation for a case (LedgerFlow v2).

    GET  /v1/categorisation?owner_ref=...       latest result + current status
    GET  /v1/categorisation/runs?owner_ref=...  run history (no results)
    POST /v1/categorisation                     {"owner_ref", "client_name"?} re-run now

Runs start by themselves when a bank statement is uploaded; POST is for a
re-run (e.g. after LedgerFlow's rules improved, or a failed run).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.categorisation_run import CategorisationRun, CategorisationStatus
from app.models.tenant import Tenant
from app.services import categorisation_service, request_service

router = APIRouter(prefix="/v1/categorisation", tags=["categorisation"])


class RunSummary(BaseModel):
    id: str
    status: str
    ledgerflow_job_id: str | None
    documents: list[Any]
    file_errors: list[Any]
    match_rate: float | None
    error: str | None
    attempts: int
    created_at: datetime
    finished_at: datetime | None


class CaseCategorisation(BaseModel):
    owner_ref: str
    enabled: bool
    # The newest run of any status: is something in progress, did it fail?
    latest: RunSummary | None
    # The newest DONE run, and LedgerFlow's ledger from it: groups -> labels ->
    # transactions (expenditure lines), plus gambling, transfers,
    # self_transfers, family_transfers, unmapped_transactions, totals, warnings.
    completed: RunSummary | None
    ledger: dict[str, Any] | None


class RerunRequest(BaseModel):
    owner_ref: str
    client_name: str | None = None


def _summary(run: CategorisationRun | None) -> RunSummary | None:
    if run is None:
        return None
    return RunSummary(
        id=str(run.id), status=run.status, ledgerflow_job_id=run.ledgerflow_job_id,
        documents=run.documents or [], file_errors=run.file_errors or [],
        match_rate=run.match_rate, error=run.error, attempts=run.attempts,
        created_at=run.created_at, finished_at=run.finished_at,
    )


@router.get("", response_model=CaseCategorisation)
async def get_case_categorisation(
    owner_ref: str = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    runs = await categorisation_service.runs_for_case(
        db, tenant_id=tenant.id, owner_ref=owner_ref)
    visible = [r for r in runs if r.status != CategorisationStatus.SUPERSEDED]
    latest = visible[0] if visible else None
    done = next((r for r in runs if r.status == CategorisationStatus.DONE), None)
    return CaseCategorisation(
        owner_ref=owner_ref, enabled=categorisation_service.enabled(),
        latest=_summary(latest), completed=_summary(done),
        ledger=done.result if done else None,
    )


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(
    owner_ref: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    runs = await categorisation_service.runs_for_case(
        db, tenant_id=tenant.id, owner_ref=owner_ref, limit=limit)
    return [_summary(r) for r in runs]


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def rerun(
    body: RerunRequest,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    if not categorisation_service.enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Categorisation is not enabled (CATEGORISER_ENABLED).")
    name = body.client_name or await request_service.client_name_for_reference(
        db, tenant=tenant, reference=body.owner_ref)
    run_id = await categorisation_service.queue_for_case(
        tenant_id=tenant.id, owner_ref=body.owner_ref, client_name=name, debounce=False)
    if run_id is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "Could not queue categorisation; see the service log.")
    return {"run_id": str(run_id), "status": CategorisationStatus.PENDING}

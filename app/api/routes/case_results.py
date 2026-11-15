"""A case's results for Katie (AI call agent): income from CAT, expenditure
from LedgerFlow, each with its proof.

    GET  /v1/case-results?owner_ref=...   both sides in one reply (what Katie reads)
    GET  /v1/cat?owner_ref=...            the CAT side: documents sent, CAT's readings, income
    POST /v1/cat  {"owner_ref"}           re-send what CAT refused and have CAT read the case again

New routes only; nothing already served changes. Same Bearer API key as /v1/*.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.categorisation_run import CategorisationStatus
from app.models.tenant import Tenant
from app.services import cat_sync_service, categorisation_service

cat_router = APIRouter(prefix="/v1/cat", tags=["cat"])
results_router = APIRouter(prefix="/v1/case-results", tags=["case-results"])


class RefreshRequest(BaseModel):
    owner_ref: str


@cat_router.get("")
async def get_cat_state(
    owner_ref: str = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return {"owner_ref": owner_ref,
            **await cat_sync_service.case_state(db, tenant_id=tenant.id, owner_ref=owner_ref)}


@cat_router.post("", status_code=status.HTTP_202_ACCEPTED)
async def refresh_cat(body: RefreshRequest, tenant: Tenant = Depends(get_current_tenant)):
    if not cat_sync_service.enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "CAT is not enabled (CAT_ENABLED).")
    run_id = await cat_sync_service.queue_refresh(tenant_id=tenant.id, owner_ref=body.owner_ref)
    return {"run_id": str(run_id), "status": "pending"}


async def _expenditure(db: AsyncSession, tenant: Tenant, owner_ref: str) -> dict[str, Any]:
    runs = await categorisation_service.runs_for_case(db, tenant_id=tenant.id,
                                                      owner_ref=owner_ref)
    visible = [r for r in runs if r.status != CategorisationStatus.SUPERSEDED]
    latest = visible[0] if visible else None
    done = next((r for r in runs if r.status == CategorisationStatus.DONE), None)
    return {
        "enabled": categorisation_service.enabled(),
        "status": latest.status if latest else None,
        "error": latest.error if latest else None,
        "finished_at": done.finished_at.isoformat() if done and done.finished_at else None,
        "documents": done.documents if done else [],
        "file_errors": done.file_errors if done else [],
        "match_rate": done.match_rate if done else None,
        # Main group -> subcategory -> transactions (proof), all-time / latest
        # month / monthly average; every row by category -> subcategory.
        "summary": done.summary if done else None,
        # LedgerFlow's ledger as CAT reads it (groups -> labels -> transactions).
        "ledger": done.result if done else None,
    }


@results_router.get("")
async def get_case_results(
    owner_ref: str = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Income (CAT) and expenditure (LedgerFlow) for one case.

    Each side has `status` (pending | processing | done | failed | null when
    nothing was sent) so Katie can tell "not yet" from "nothing to show".
    """
    cat = await cat_sync_service.case_state(db, tenant_id=tenant.id, owner_ref=owner_ref)
    return {
        "owner_ref": owner_ref,
        "income": {k: cat[k] for k in ("enabled", "cat_case", "status", "error",
                                       "finished_at", "documents", "income")},
        "expenditure": await _expenditure(db, tenant, owner_ref),
    }

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CategorisationStatus(str):
    """Lifecycle of one categorisation of a case's bank statements.

    PENDING    -> recorded; waiting out the debounce before sending.
    PROCESSING -> sent to LedgerFlow; `ledgerflow_job_id` is being polled.
    DONE       -> `result` holds LedgerFlow's ledger for the case.
    FAILED     -> `error` says why (unreadable statement, LedgerFlow down...).
    SUPERSEDED -> a newer run for the same case replaced this one before it
                  was sent (another statement arrived during the debounce).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"

    UNFINISHED = (PENDING, PROCESSING)


class CategorisationRun(Base):
    """One LedgerFlow job covering every bank statement on a case.

    Keyed by case, not by document: LedgerFlow finds duplicate rows and
    overlapping periods only across the files of a single job, and its ledger
    is the case's whole picture. The latest DONE run is the case's answer.
    """

    __tablename__ = "categorisation_run"
    __table_args__ = (
        Index("categorisation_run_case_idx", "tenant_id", "owner_ref", "created_at"),
        Index("categorisation_run_status_idx", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    owner_ref: Mapped[str] = mapped_column(String, nullable=False)
    # The account holder, for LedgerFlow's own-name (self-transfer) check.
    client_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )
    ledgerflow_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The documents sent, as [{"id", "file_name"}] -- what the result covers.
    documents: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # LedgerFlow's /ledger reply: groups -> labels -> transactions, transfers,
    # gambling, unmapped, totals, warnings.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # LedgerFlow's per-file read errors and warnings, kept even on success.
    file_errors: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # LedgerFlow's /summary: SFS group -> label -> transactions with all-time,
    # latest-month and monthly figures; every row by category -> subcategory.
    # None when LedgerFlow has no /summary route (older deploy): ledger only.
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    match_rate: Mapped[float | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

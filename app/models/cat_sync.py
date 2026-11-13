import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CatDocumentStatus(str):
    """One document's way into CAT.

    PENDING       -> waiting to be uploaded to CAT.
    SENT          -> CAT has the file; waiting for CAT to read it.
    PROCESSED     -> CAT read it (`reading` holds what it found).
    READ_FAILED   -> CAT could not read it.
    READ_TIMEOUT  -> CAT had not read it when we stopped waiting (a re-run asks again).
    FAILED        -> CAT refused the upload (`error`: case not in CAT, locked, ...).
    """

    PENDING = "pending"
    SENT = "sent"
    PROCESSED = "processed"
    READ_FAILED = "read_failed"
    READ_TIMEOUT = "read_timeout"
    FAILED = "failed"


class CatRunStatus(str):
    """One "send what's new, have CAT read the case, collect income" pass.

    PENDING -> PROCESSING -> DONE | FAILED;  SUPERSEDED when a newer run for
    the same case was queued during the debounce (it carries this one's files).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"

    UNFINISHED = (PENDING, PROCESSING)


class CatDocument(Base):
    """A document of this service, as sent to CAT."""

    __tablename__ = "cat_document"
    __table_args__ = (
        Index("cat_document_case_idx", "tenant_id", "owner_ref"),
        Index("cat_document_doc_idx", "document_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document.id", ondelete="CASCADE"), nullable=False)
    owner_ref: Mapped[str] = mapped_column(String, nullable=False)
    cat_case: Mapped[str] = mapped_column(String, nullable=False)
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    cat_document_type: Mapped[str] = mapped_column(String, nullable=False)
    # The unique name the file has in CAT -- how we find it among the case's documents.
    cat_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    cat_reply: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reading: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CatRun(Base):
    """A pass over one case in CAT. The newest DONE run holds the case's income."""

    __tablename__ = "cat_run"
    __table_args__ = (
        Index("cat_run_case_idx", "tenant_id", "owner_ref", "created_at"),
        Index("cat_run_status_idx", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False)
    owner_ref: Mapped[str] = mapped_column(String, nullable=False)
    cat_case: Mapped[str] = mapped_column(String, nullable=False)
    # 'upload' (a document arrived) or 'manual' (POST /v1/cat: read again).
    trigger: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'upload'"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    # {"income": [...Proof of Income rows...], "evidence_ledger": [...], "documents": [...]}
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

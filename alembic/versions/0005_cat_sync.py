"""cat_document + cat_run tables (documents -> CAT), categorisation_run.summary

Only ADDS: two new tables and one nullable column. No existing row or column
is changed, so every endpoint already in use answers exactly as before.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "cat_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_ref", sa.Text(), nullable=False),
        sa.Column("cat_case", sa.Text(), nullable=False),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("cat_document_type", sa.Text(), nullable=False),
        sa.Column("cat_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("cat_reply", postgresql.JSONB(), nullable=True),
        sa.Column("reading", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_stamps(),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("cat_document_case_idx", "cat_document", ["tenant_id", "owner_ref"])
    op.create_index("cat_document_doc_idx", "cat_document", ["document_id"], unique=True)

    op.create_table(
        "cat_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_ref", sa.Text(), nullable=False),
        sa.Column("cat_case", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), server_default=sa.text("'upload'"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        *_stamps(),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("cat_run_case_idx", "cat_run", ["tenant_id", "owner_ref", "created_at"])
    op.create_index("cat_run_status_idx", "cat_run", ["status"])

    # LedgerFlow's /summary (group -> label -> transactions, latest month,
    # category -> subcategory) next to the ledger it is built from.
    op.add_column("categorisation_run", sa.Column("summary", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("categorisation_run", "summary")
    op.drop_index("cat_run_status_idx", table_name="cat_run")
    op.drop_index("cat_run_case_idx", table_name="cat_run")
    op.drop_table("cat_run")
    op.drop_index("cat_document_doc_idx", table_name="cat_document")
    op.drop_index("cat_document_case_idx", table_name="cat_document")
    op.drop_table("cat_document")

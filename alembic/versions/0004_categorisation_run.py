"""categorisation_run table (bank statements -> LedgerFlow)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "categorisation_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_ref", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("ledgerflow_job_id", sa.Text(), nullable=True),
        sa.Column("documents", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"),
                  nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("file_errors", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"),
                  nullable=False),
        sa.Column("match_rate", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("categorisation_run_case_idx", "categorisation_run",
                    ["tenant_id", "owner_ref", "created_at"])
    op.create_index("categorisation_run_status_idx", "categorisation_run", ["status"])


def downgrade() -> None:
    op.drop_index("categorisation_run_status_idx", table_name="categorisation_run")
    op.drop_index("categorisation_run_case_idx", table_name="categorisation_run")
    op.drop_table("categorisation_run")

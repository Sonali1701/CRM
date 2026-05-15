"""add outreach classification cache columns to leads

Revision ID: f9a2b3c4d5e6
Revises: e8f1a2b3c4d5
Create Date: 2026-05-15 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9a2b3c4d5e6"
down_revision: Union[str, None] = "e8f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("outreach_category", sa.String(length=40), nullable=True))
    op.create_index("ix_leads_outreach_category", "leads", ["outreach_category"])
    op.add_column("leads", sa.Column("outreach_summary", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("outreach_suggested_poc", sa.String(length=200), nullable=True))
    op.add_column("leads", sa.Column("outreach_reconnect_date", sa.Date(), nullable=True))
    op.add_column("leads", sa.Column("outreach_notes_hash", sa.String(length=64), nullable=True))
    op.add_column("leads", sa.Column("outreach_classified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "outreach_classified_at")
    op.drop_column("leads", "outreach_notes_hash")
    op.drop_column("leads", "outreach_reconnect_date")
    op.drop_column("leads", "outreach_suggested_poc")
    op.drop_column("leads", "outreach_summary")
    op.drop_index("ix_leads_outreach_category", table_name="leads")
    op.drop_column("leads", "outreach_category")

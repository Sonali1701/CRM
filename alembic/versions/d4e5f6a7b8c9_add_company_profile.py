"""add company profile singleton

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-13 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default="Radixsol"),
        sa.Column("website", sa.String(length=500), nullable=False, server_default="https://radixsol.com"),
        sa.Column("tagline", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("services", sa.Text(), nullable=False, server_default=""),
        sa.Column("tone_guidelines", sa.Text(), nullable=False, server_default=""),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("website_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("website_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Seed the singleton row so callers can always assume id=1 exists.
    op.execute(
        "INSERT INTO company_profile (id, name, website, tagline, description, services, "
        "tone_guidelines, signature, website_excerpt, updated_at) VALUES "
        "(1, 'Radixsol', 'https://radixsol.com', '', '', '', '', '', '', CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.drop_table("company_profile")

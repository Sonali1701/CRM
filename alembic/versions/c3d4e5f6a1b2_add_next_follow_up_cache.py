"""add next_follow_up_at cache to leads

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a1
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a1b2'
down_revision = 'b2c3d4e5f6a1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('leads', sa.Column('next_follow_up_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_leads_next_follow_up_at', 'leads', ['next_follow_up_at'])


def downgrade():
    op.drop_index('ix_leads_next_follow_up_at', table_name='leads')
    op.drop_column('leads', 'next_follow_up_at')

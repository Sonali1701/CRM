"""add last_file_hash to auto_sync_configs

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a1b2c3
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a1b2c3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'auto_sync_configs',
        sa.Column('last_file_hash', sa.String(64), nullable=True)
    )


def downgrade():
    op.drop_column('auto_sync_configs', 'last_file_hash')

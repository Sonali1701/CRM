"""add auto_sync_configs

Revision ID: b2c3d4e5f6a1
Revises: 84df91a5f67b
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a1'
down_revision = '84df91a5f67b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'auto_sync_configs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('sheet_name', sa.String(255)),
        sa.Column('column_mapping', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('sync_interval_hours', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('last_synced_at', sa.DateTime(timezone=True)),
        sa.Column('last_result', sa.Text()),
        sa.Column('last_error', sa.Text()),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table('auto_sync_configs')

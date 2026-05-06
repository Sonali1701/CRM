"""add contact and company fields

Revision ID: c9f1d2e3b4a5
Revises: 20e4d0c4dab3
Create Date: 2026-05-06 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9f1d2e3b4a5'
down_revision: Union[str, None] = '20e4d0c4dab3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('leads') as batch_op:
        batch_op.alter_column('name', new_column_name='first_name')
        batch_op.add_column(sa.Column('last_name', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('job_title', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('linkedin_url', sa.String(500), nullable=True))
        batch_op.add_column(sa.Column('mobile', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('city', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('state', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('country', sa.String(100), nullable=True))

    with op.batch_alter_table('clients') as batch_op:
        batch_op.add_column(sa.Column('hq_city', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('hq_state', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('hq_country', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('phone', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('email', sa.String(255), nullable=True))
        batch_op.add_column(sa.Column('linkedin_url', sa.String(500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('clients') as batch_op:
        batch_op.drop_column('linkedin_url')
        batch_op.drop_column('email')
        batch_op.drop_column('phone')
        batch_op.drop_column('hq_country')
        batch_op.drop_column('hq_state')
        batch_op.drop_column('hq_city')

    with op.batch_alter_table('leads') as batch_op:
        batch_op.drop_column('country')
        batch_op.drop_column('state')
        batch_op.drop_column('city')
        batch_op.drop_column('mobile')
        batch_op.drop_column('linkedin_url')
        batch_op.drop_column('job_title')
        batch_op.drop_column('last_name')
        batch_op.alter_column('first_name', new_column_name='name')

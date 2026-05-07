"""add company description

Revision ID: d2a4f8b1c0e3
Revises: c9f1d2e3b4a5
Create Date: 2026-05-07 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2a4f8b1c0e3'
down_revision: Union[str, None] = 'c9f1d2e3b4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('clients') as batch_op:
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('clients') as batch_op:
        batch_op.drop_column('description')

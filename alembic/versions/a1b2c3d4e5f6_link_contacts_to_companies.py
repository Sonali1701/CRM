"""link contacts to companies

Revision ID: a1b2c3d4e5f6
Revises: e7b1c2d3f4a5
Create Date: 2026-05-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e7b1c2d3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leads") as batch_op:
        batch_op.add_column(sa.Column("client_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_leads_client_id", "clients", ["client_id"], ["id"])
        batch_op.create_index("ix_leads_client_id", ["client_id"])


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch_op:
        batch_op.drop_index("ix_leads_client_id")
        batch_op.drop_constraint("fk_leads_client_id", type_="foreignkey")
        batch_op.drop_column("client_id")

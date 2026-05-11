"""widen email_messages and activities varchar columns

Graph message IDs / conversation IDs / real-world email subjects routinely
exceed VARCHAR(255); on Postgres this raises StringDataRightTruncation.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-12 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("email_messages") as batch:
        batch.alter_column("subject", existing_type=sa.String(255), type_=sa.String(500), existing_nullable=True)
        batch.alter_column("from_email", existing_type=sa.String(255), type_=sa.String(320), existing_nullable=True)
        batch.alter_column("provider_message_id", existing_type=sa.String(255), type_=sa.String(1000), existing_nullable=False)
        batch.alter_column("internet_message_id", existing_type=sa.String(255), type_=sa.String(1000), existing_nullable=True)
        batch.alter_column("conversation_id", existing_type=sa.String(255), type_=sa.String(500), existing_nullable=True)

    with op.batch_alter_table("activities") as batch:
        batch.alter_column("subject", existing_type=sa.String(255), type_=sa.String(500), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("activities") as batch:
        batch.alter_column("subject", type_=sa.String(255), existing_type=sa.String(500), existing_nullable=False)

    with op.batch_alter_table("email_messages") as batch:
        batch.alter_column("conversation_id", type_=sa.String(255), existing_type=sa.String(500), existing_nullable=True)
        batch.alter_column("internet_message_id", type_=sa.String(255), existing_type=sa.String(1000), existing_nullable=True)
        batch.alter_column("provider_message_id", type_=sa.String(255), existing_type=sa.String(1000), existing_nullable=False)
        batch.alter_column("from_email", type_=sa.String(255), existing_type=sa.String(320), existing_nullable=True)
        batch.alter_column("subject", type_=sa.String(255), existing_type=sa.String(500), existing_nullable=True)

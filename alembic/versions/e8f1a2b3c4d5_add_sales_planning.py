"""add sales planning: MEDDIC, account plan, close plan + steps

Revision ID: e8f1a2b3c4d5
Revises: d4e5f6a7b8c9
Create Date: 2026-05-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e8f1a2b3c4d5"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deal_qualifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.Integer(), nullable=False),
        sa.Column("metrics_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics_notes", sa.Text(), nullable=True),
        sa.Column("economic_buyer_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("economic_buyer_notes", sa.Text(), nullable=True),
        sa.Column("decision_criteria_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision_criteria_notes", sa.Text(), nullable=True),
        sa.Column("decision_process_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision_process_notes", sa.Text(), nullable=True),
        sa.Column("identify_pain_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("identify_pain_notes", sa.Text(), nullable=True),
        sa.Column("champion_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("champion_notes", sa.Text(), nullable=True),
        sa.Column("last_scored_by_id", sa.Integer(), nullable=True),
        sa.Column("last_scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scored_by_ai", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_scored_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deal_id"),
    )
    op.create_index("ix_deal_qualifications_deal_id", "deal_qualifications", ["deal_id"])

    op.create_table(
        "account_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("business_goals", sa.Text(), nullable=True),
        sa.Column("whitespace", sa.Text(), nullable=True),
        sa.Column("key_stakeholders", sa.Text(), nullable=True),
        sa.Column("threats_risks", sa.Text(), nullable=True),
        sa.Column("next_90d_actions", sa.Text(), nullable=True),
        sa.Column("success_metrics", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id"),
    )
    op.create_index("ix_account_plans_client_id", "account_plans", ["client_id"])

    op.create_table(
        "close_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deal_id", sa.Integer(), nullable=False),
        sa.Column("target_close_date", sa.Date(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deal_id"),
    )
    op.create_index("ix_close_plans_deal_id", "close_plans", ["deal_id"])

    op.create_table(
        "close_plan_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("close_plan_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("owner_label", sa.String(length=120), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.Enum("PENDING", "IN_PROGRESS", "DONE", "BLOCKED", name="closeplanstepstatus"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["close_plan_id"], ["close_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_close_plan_steps_close_plan_id", "close_plan_steps", ["close_plan_id"])


def downgrade() -> None:
    op.drop_index("ix_close_plan_steps_close_plan_id", table_name="close_plan_steps")
    op.drop_table("close_plan_steps")
    op.drop_index("ix_close_plans_deal_id", table_name="close_plans")
    op.drop_table("close_plans")
    op.drop_index("ix_account_plans_client_id", table_name="account_plans")
    op.drop_table("account_plans")
    op.drop_index("ix_deal_qualifications_deal_id", table_name="deal_qualifications")
    op.drop_table("deal_qualifications")

"""add mail accounts and email messages

Revision ID: e7b1c2d3f4a5
Revises: d2a4f8b1c0e3
Create Date: 2026-05-08 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b1c2d3f4a5"
down_revision: Union[str, None] = "d2a4f8b1c0e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("mailbox", sa.String(length=255), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inbox_delta_link", sa.Text(), nullable=True),
        sa.Column("sent_delta_link", sa.Text(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_mail_accounts_user_provider"),
    )
    op.create_index(op.f("ix_mail_accounts_provider"), "mail_accounts", ["provider"], unique=False)
    op.create_index(op.f("ix_mail_accounts_mailbox"), "mail_accounts", ["mailbox"], unique=False)
    op.create_index(op.f("ix_mail_accounts_user_id"), "mail_accounts", ["user_id"], unique=False)

    op.create_table(
        "email_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mail_account_id", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(length=30), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("internet_message_id", sa.String(length=255), nullable=True),
        sa.Column("conversation_id", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("body_preview", sa.Text(), nullable=True),
        sa.Column("body_content", sa.Text(), nullable=True),
        sa.Column("from_email", sa.String(length=255), nullable=True),
        sa.Column("to_emails", sa.Text(), nullable=True),
        sa.Column("cc_emails", sa.Text(), nullable=True),
        sa.Column("is_inbound", sa.Boolean(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("lead_id", sa.Integer(), nullable=True),
        sa.Column("deal_id", sa.Integer(), nullable=True),
        sa.Column("activity_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["mail_account_id"], ["mail_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mail_account_id",
            "provider_message_id",
            name="uq_email_messages_account_message",
        ),
    )

    op.create_index(op.f("ix_email_messages_activity_id"), "email_messages", ["activity_id"], unique=False)
    op.create_index(op.f("ix_email_messages_client_id"), "email_messages", ["client_id"], unique=False)
    op.create_index(op.f("ix_email_messages_conversation_id"), "email_messages", ["conversation_id"], unique=False)
    op.create_index(op.f("ix_email_messages_deal_id"), "email_messages", ["deal_id"], unique=False)
    op.create_index(op.f("ix_email_messages_folder"), "email_messages", ["folder"], unique=False)
    op.create_index(op.f("ix_email_messages_from_email"), "email_messages", ["from_email"], unique=False)
    op.create_index(op.f("ix_email_messages_internet_message_id"), "email_messages", ["internet_message_id"], unique=False)
    op.create_index(op.f("ix_email_messages_is_inbound"), "email_messages", ["is_inbound"], unique=False)
    op.create_index(op.f("ix_email_messages_lead_id"), "email_messages", ["lead_id"], unique=False)
    op.create_index(op.f("ix_email_messages_mail_account_id"), "email_messages", ["mail_account_id"], unique=False)
    op.create_index(op.f("ix_email_messages_provider_message_id"), "email_messages", ["provider_message_id"], unique=False)
    op.create_index(op.f("ix_email_messages_received_at"), "email_messages", ["received_at"], unique=False)
    op.create_index(op.f("ix_email_messages_sent_at"), "email_messages", ["sent_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_email_messages_sent_at"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_received_at"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_provider_message_id"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_mail_account_id"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_lead_id"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_is_inbound"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_internet_message_id"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_from_email"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_folder"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_deal_id"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_conversation_id"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_client_id"), table_name="email_messages")
    op.drop_index(op.f("ix_email_messages_activity_id"), table_name="email_messages")
    op.drop_table("email_messages")

    op.drop_index(op.f("ix_mail_accounts_user_id"), table_name="mail_accounts")
    op.drop_index(op.f("ix_mail_accounts_mailbox"), table_name="mail_accounts")
    op.drop_index(op.f("ix_mail_accounts_provider"), table_name="mail_accounts")
    op.drop_table("mail_accounts")

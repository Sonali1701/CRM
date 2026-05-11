from datetime import datetime, timezone

from sqlalchemy import String, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MailAccount(Base):
    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    provider: Mapped[str] = mapped_column(String(50), default="microsoft_graph", index=True)
    mailbox: Mapped[str] = mapped_column(String(255), index=True)

    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    inbox_delta_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_delta_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_mail_accounts_user_provider"),
    )


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    mail_account_id: Mapped[int] = mapped_column(ForeignKey("mail_accounts.id"), index=True)

    folder: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)  # inbox/sent

    provider_message_id: Mapped[str] = mapped_column(String(255), index=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    from_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    to_emails: Mapped[str | None] = mapped_column(Text, nullable=True)
    cc_emails: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_inbound: Mapped[bool] = mapped_column(default=True, index=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True, index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"), nullable=True, index=True)

    activity_id: Mapped[int | None] = mapped_column(ForeignKey("activities.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    mail_account = relationship("MailAccount")

    __table_args__ = (
        UniqueConstraint("mail_account_id", "provider_message_id", name="uq_email_messages_account_message"),
    )

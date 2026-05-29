from datetime import datetime, timezone, date

from sqlalchemy import Date, Integer, ForeignKey, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    client_name: Mapped[str | None] = mapped_column(Text)
    accounts_worked: Mapped[int] = mapped_column(Integer, default=0)
    emails_sent: Mapped[int] = mapped_column(Integer, default=0)
    calls_dialed: Mapped[int] = mapped_column(Integer, default=0)
    meetings_set: Mapped[int] = mapped_column(Integer, default=0)
    meetings_attended: Mapped[int] = mapped_column(Integer, default=0)
    linkedin_requests_sent: Mapped[int] = mapped_column(Integer, default=0)
    linkedin_connections: Mapped[int] = mapped_column(Integer, default=0)
    important_conversations: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])

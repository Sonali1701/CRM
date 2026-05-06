import enum
from datetime import datetime, timezone

from sqlalchemy import String, Enum, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class LeadStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    CONVERTED = "converted"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Name split into first + last
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Professional info
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Contact info
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    mobile: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Location
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # CRM fields
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.NEW, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    converted_client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner = relationship("User", back_populates="leads", foreign_keys=[owner_id])
    converted_client = relationship("Client", foreign_keys=[converted_client_id])

    @property
    def name(self) -> str:
        """Full name — keeps backward compat with all existing code."""
        parts = [self.first_name]
        if self.last_name:
            parts.append(self.last_name)
        return " ".join(parts)

    @property
    def location(self) -> str:
        parts = [p for p in [self.city, self.state, self.country] if p]
        return ", ".join(parts)

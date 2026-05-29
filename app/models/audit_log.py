"""
Audit log for tracking user actions (lead creation, imports, note updates, etc.).
Used by admin to monitor team performance.
"""
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(50), index=True)  # e.g., "create_lead", "import_leads", "update_notes"
    entity_type: Mapped[str] = mapped_column(String(50))  # e.g., "lead", "import"
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # lead_id, import_id, etc.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {count: 10, source: "excel", etc.}
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    user = relationship("User", foreign_keys=[user_id])

    @property
    def summary(self) -> str:
        """Human-readable summary of the action."""
        if self.action == "create_lead":
            return f"Created lead: {self.details.get('name', 'Unknown')}"
        elif self.action == "import_leads":
            count = self.details.get("count", 0)
            return f"Imported {count} lead{'s' if count != 1 else ''}"
        elif self.action == "update_notes":
            return f"Updated notes on lead {self.entity_id}"
        elif self.action == "excel_sync":
            return f"Synced Excel: {self.details.get('created', 0)} created, {self.details.get('updated', 0)} updated"
        return self.action

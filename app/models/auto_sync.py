import json
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AutoSyncConfig(Base):
    __tablename__ = "auto_sync_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    sheet_name: Mapped[str | None] = mapped_column(String(255))
    # JSON-encoded {crm_field: excel_column}
    column_mapping: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sync_interval_hours: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # JSON summary of last run: {created, updated, activities, skipped} counts
    last_result: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_file_hash: Mapped[str | None] = mapped_column(String(64))
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    owner = relationship("User", foreign_keys=[owner_id])

    @property
    def mapping_dict(self) -> dict:
        try:
            return json.loads(self.column_mapping or "{}")
        except Exception:
            return {}

    @property
    def last_result_dict(self) -> dict:
        try:
            return json.loads(self.last_result or "{}")
        except Exception:
            return {}

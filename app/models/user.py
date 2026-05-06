import enum
from datetime import datetime, timezone

from sqlalchemy import String, Enum, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    SALES = "sales"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.SALES)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    leads = relationship("Lead", back_populates="owner", foreign_keys="Lead.owner_id")
    deals = relationship("Deal", back_populates="owner", foreign_keys="Deal.owner_id")
    activities = relationship("Activity", back_populates="created_by", foreign_keys="Activity.created_by_id")

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def is_manager(self) -> bool:
        return self.role in (UserRole.ADMIN, UserRole.MANAGER)

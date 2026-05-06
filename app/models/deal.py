import enum
from datetime import datetime, timezone, date

from sqlalchemy import String, Enum, DateTime, ForeignKey, Text, Numeric, Date, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DealStage(str, enum.Enum):
    LEAD_GENERATED = "lead_generated"
    QUALIFIED = "qualified"
    DISCOVERY_DONE = "discovery_done"
    REQUIREMENT_RECEIVED = "requirement_received"
    PROPOSAL_SHARED = "proposal_shared"
    NEGOTIATION = "negotiation"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


STAGE_LABELS = {
    DealStage.LEAD_GENERATED: "Lead Generated",
    DealStage.QUALIFIED: "Qualified",
    DealStage.DISCOVERY_DONE: "Discovery Done",
    DealStage.REQUIREMENT_RECEIVED: "Requirement Received",
    DealStage.PROPOSAL_SHARED: "Proposal Shared",
    DealStage.NEGOTIATION: "Negotiation",
    DealStage.CLOSED_WON: "Closed Won",
    DealStage.CLOSED_LOST: "Closed Lost",
}

OPEN_STAGES = [
    DealStage.LEAD_GENERATED,
    DealStage.QUALIFIED,
    DealStage.DISCOVERY_DONE,
    DealStage.REQUIREMENT_RECEIVED,
    DealStage.PROPOSAL_SHARED,
    DealStage.NEGOTIATION,
]

PIPELINE_STAGES = OPEN_STAGES + [DealStage.CLOSED_WON, DealStage.CLOSED_LOST]


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    value: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    stage: Mapped[DealStage] = mapped_column(Enum(DealStage), default=DealStage.LEAD_GENERATED, index=True)
    stage_position: Mapped[int] = mapped_column(Integer, default=0)
    expected_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    probability: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client = relationship("Client", back_populates="deals")
    owner = relationship("User", back_populates="deals", foreign_keys=[owner_id])
    activities = relationship("Activity", back_populates="deal", cascade="all, delete-orphan")

    @property
    def is_open(self) -> bool:
        return self.stage in OPEN_STAGES

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage.value)

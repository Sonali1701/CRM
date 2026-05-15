"""Sales-team planning artifacts: MEDDIC qualification per deal,
Account Plan per client, Close Plan + Steps per deal."""

import enum
from datetime import datetime, timezone, date

from sqlalchemy import String, DateTime, Date, ForeignKey, Text, Integer, Enum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# Per-dimension MEDDIC score (0 = unknown / 1 = weak / 2 = partial / 3 = strong)
MEDDIC_DIMENSIONS = (
    "metrics",
    "economic_buyer",
    "decision_criteria",
    "decision_process",
    "identify_pain",
    "champion",
)


class DealQualification(Base):
    """MEDDIC scoring for a deal. One row per deal."""

    __tablename__ = "deal_qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), unique=True, index=True)

    metrics_score: Mapped[int] = mapped_column(Integer, default=0)
    metrics_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    economic_buyer_score: Mapped[int] = mapped_column(Integer, default=0)
    economic_buyer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision_criteria_score: Mapped[int] = mapped_column(Integer, default=0)
    decision_criteria_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision_process_score: Mapped[int] = mapped_column(Integer, default=0)
    decision_process_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    identify_pain_score: Mapped[int] = mapped_column(Integer, default=0)
    identify_pain_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    champion_score: Mapped[int] = mapped_column(Integer, default=0)
    champion_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_scored_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    last_scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scored_by_ai: Mapped[int] = mapped_column(Integer, default=0)  # 0/1 — was the last update AI-driven

    deal = relationship("Deal", foreign_keys=[deal_id])
    last_scored_by = relationship("User", foreign_keys=[last_scored_by_id])

    @property
    def total_score(self) -> int:
        return (
            self.metrics_score + self.economic_buyer_score + self.decision_criteria_score
            + self.decision_process_score + self.identify_pain_score + self.champion_score
        )

    @property
    def total_max(self) -> int:
        return len(MEDDIC_DIMENSIONS) * 3

    @property
    def overall_band(self) -> str:
        """Strong / Average / Weak band based on total score."""
        pct = self.total_score / self.total_max if self.total_max else 0
        if pct >= 0.66:
            return "strong"
        if pct >= 0.33:
            return "average"
        return "weak"


class AccountPlan(Base):
    """Strategic account plan for a top client. One row per client."""

    __tablename__ = "account_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), unique=True, index=True)

    business_goals: Mapped[str | None] = mapped_column(Text, nullable=True)
    whitespace: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_stakeholders: Mapped[str | None] = mapped_column(Text, nullable=True)
    threats_risks: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_90d_actions: Mapped[str | None] = mapped_column(Text, nullable=True)
    success_metrics: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    client = relationship("Client", foreign_keys=[client_id])
    created_by = relationship("User", foreign_keys=[created_by_id])


class ClosePlanStepStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class ClosePlan(Base):
    """Mutual close plan for a deal — agreed roadmap to signature.
    One row per deal; steps are a child collection."""

    __tablename__ = "close_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), unique=True, index=True)

    target_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    deal = relationship("Deal", foreign_keys=[deal_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    steps = relationship(
        "ClosePlanStep",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="ClosePlanStep.position",
    )

    @property
    def progress_pct(self) -> int:
        if not self.steps:
            return 0
        done = sum(1 for s in self.steps if s.status == ClosePlanStepStatus.DONE)
        return int(done / len(self.steps) * 100)


class ClosePlanStep(Base):
    __tablename__ = "close_plan_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    close_plan_id: Mapped[int] = mapped_column(ForeignKey("close_plans.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    title: Mapped[str] = mapped_column(String(500))
    owner_label: Mapped[str | None] = mapped_column(String(120), nullable=True)  # "us" / "client" / rep name
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[ClosePlanStepStatus] = mapped_column(Enum(ClosePlanStepStatus), default=ClosePlanStepStatus.PENDING)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    plan = relationship("ClosePlan", back_populates="steps")

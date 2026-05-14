"""Per-deal risk scoring — pure rules, no AI.

Returns one of "high" / "medium" / "low" / "ok" based on activity gap,
age, and deal value. AI-explained on demand via /ai-tools/risk/{id}."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import Deal
from app.models.deal import OPEN_STAGES


@dataclass
class DealRisk:
    level: str            # "high" | "medium" | "low" | "ok"
    days_since_activity: int | None
    days_in_stage: int | None
    reasons: list[str]    # short rule-based reasons


def _aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def compute_risk(deal: Deal, *, stale_days: int = 5, slow_stage_days: int = 21) -> DealRisk:
    """Score a deal based on rule signals. Closed deals are always 'ok'."""
    if deal.stage not in OPEN_STAGES:
        return DealRisk(level="ok", days_since_activity=None, days_in_stage=None, reasons=[])

    now = datetime.now(timezone.utc)
    last_act = _aware(deal.last_activity_at) or _aware(deal.updated_at)
    days_since = (now - last_act).days if last_act else None
    days_in_stage = (now - _aware(deal.updated_at)).days if deal.updated_at else None
    value = float(deal.value or 0)

    reasons: list[str] = []
    if days_since is not None and days_since >= stale_days * 2:
        reasons.append(f"No activity for {days_since} days")
    elif days_since is not None and days_since >= stale_days:
        reasons.append(f"Last activity {days_since} days ago")
    if days_in_stage is not None and days_in_stage >= slow_stage_days:
        reasons.append(f"Stuck in '{deal.stage_label}' for {days_in_stage} days")
    if value >= 50000 and days_since is not None and days_since >= stale_days:
        reasons.append(f"High-value ({value:,.0f} {deal.currency}) with no recent touch")

    # Bucket the level
    if days_since is None:
        level = "ok"
    elif days_since >= stale_days * 3 or (value >= 50000 and days_since >= stale_days * 2):
        level = "high"
    elif days_since >= stale_days or (days_in_stage and days_in_stage >= slow_stage_days):
        level = "medium"
    elif days_since >= stale_days // 2 + 1:
        level = "low"
    else:
        level = "ok"

    return DealRisk(
        level=level,
        days_since_activity=days_since,
        days_in_stage=days_in_stage,
        reasons=reasons,
    )


def risk_context_string(deal: Deal, risk: DealRisk) -> str:
    """Plain-text dump for the AI risk-explainer prompt."""
    lines = [
        f"Deal: {deal.title}",
        f"Stage: {deal.stage_label}",
        f"Value: {deal.value} {deal.currency}",
        f"Probability: {deal.probability}%",
        f"Risk level (computed): {risk.level}",
    ]
    if risk.days_since_activity is not None:
        lines.append(f"Days since last activity: {risk.days_since_activity}")
    if risk.days_in_stage is not None:
        lines.append(f"Days in current stage: {risk.days_in_stage}")
    if risk.reasons:
        lines.append("Risk signals:")
        lines.extend(f"  - {r}" for r in risk.reasons)
    if deal.notes:
        lines.append(f"\nNotes:\n{deal.notes[:600]}")
    return "\n".join(lines)

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.flash import get_flash
from app.models import User, Deal, Activity
from app.models.deal import DealStage, OPEN_STAGES, STAGE_LABELS
from app.templating import templates

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    stale_days = get_settings().stale_deal_days
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    base_q = db.query(Deal)
    if not user.is_manager:
        base_q = base_q.filter(Deal.owner_id == user.id)

    open_deals = base_q.filter(Deal.stage.in_(OPEN_STAGES)).all()

    pipeline_value = sum(float(d.value or 0) for d in open_deals)
    expected_revenue = sum(
        float(d.value or 0) * (d.probability / 100)
        for d in open_deals
        if d.expected_close_date and d.expected_close_date.month == datetime.now().month
    )

    by_stage = {}
    for stage in OPEN_STAGES:
        deals_in_stage = [d for d in open_deals if d.stage == stage]
        by_stage[stage] = {
            "label": STAGE_LABELS[stage],
            "count": len(deals_in_stage),
            "value": sum(float(d.value or 0) for d in deals_in_stage),
        }

    stale_deals = [
        d for d in open_deals
        if (d.last_activity_at is None and (
            d.updated_at.replace(tzinfo=timezone.utc) if d.updated_at.tzinfo is None else d.updated_at
        ) < stale_cutoff)
        or (d.last_activity_at is not None and (
            d.last_activity_at.replace(tzinfo=timezone.utc) if d.last_activity_at.tzinfo is None else d.last_activity_at
        ) < stale_cutoff)
    ]

    upcoming_q = db.query(Activity).filter(
        Activity.completed == False,
        Activity.due_at >= datetime.now(timezone.utc),
    )
    if not user.is_manager:
        upcoming_q = upcoming_q.filter(Activity.created_by_id == user.id)
    upcoming_activities = upcoming_q.order_by(Activity.due_at).limit(5).all()

    overdue_q = db.query(Activity).filter(
        Activity.completed == False,
        Activity.due_at < datetime.now(timezone.utc),
        Activity.due_at.isnot(None),
    )
    if not user.is_manager:
        overdue_q = overdue_q.filter(Activity.created_by_id == user.id)
    overdue_activities = overdue_q.order_by(Activity.due_at).all()

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "flash": get_flash(request),
        "pipeline_value": pipeline_value,
        "expected_revenue": expected_revenue,
        "by_stage": by_stage,
        "open_deals": open_deals,
        "stale_deals": stale_deals,
        "stale_days": stale_days,
        "upcoming_activities": upcoming_activities,
        "overdue_activities": overdue_activities,
        "OPEN_STAGES": OPEN_STAGES,
    })

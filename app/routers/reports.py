"""Reporting dashboard: pipeline value by stage, win rate, rep leaderboard,
conversion funnel, recent activity volume.

Managers see org-wide numbers; reps see only their own."""

from datetime import datetime, timedelta, timezone
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import get_flash
from app.models import User, Deal, Activity, Lead
from app.models.deal import DealStage, OPEN_STAGES, STAGE_LABELS
from app.models.activity import ActivityType
from app.models.lead import LeadStatus
from app.templating import templates


router = APIRouter()


@router.get("/reports")
def reports(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - timedelta(days=30)

    # Scope: managers see all, reps see only their own
    deals_q = db.query(Deal)
    acts_q = db.query(Activity)
    if not user.is_manager:
        deals_q = deals_q.filter(Deal.owner_id == user.id)
        acts_q = acts_q.filter(Activity.created_by_id == user.id)

    all_deals = deals_q.all()
    open_deals = [d for d in all_deals if d.stage in OPEN_STAGES]
    def _aware(dt):
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    won = [d for d in all_deals if d.stage == DealStage.CLOSED_WON]
    lost = [d for d in all_deals if d.stage == DealStage.CLOSED_LOST]
    won_this_month = [d for d in won if _aware(d.updated_at) and _aware(d.updated_at) >= month_start]

    pipeline_value = sum(float(d.value or 0) for d in open_deals)
    won_value_total = sum(float(d.value or 0) for d in won)
    won_value_month = sum(float(d.value or 0) for d in won_this_month)

    closed_total = len(won) + len(lost)
    win_rate = (len(won) / closed_total * 100) if closed_total else 0

    # Pipeline by stage (count + value)
    by_stage = []
    max_stage_value = 1
    for stage in OPEN_STAGES:
        ds = [d for d in open_deals if d.stage == stage]
        v = sum(float(d.value or 0) for d in ds)
        max_stage_value = max(max_stage_value, v)
        by_stage.append({
            "label": STAGE_LABELS[stage],
            "stage": stage.value,
            "count": len(ds),
            "value": v,
        })
    for row in by_stage:
        row["pct"] = int(row["value"] / max_stage_value * 100) if max_stage_value else 0

    # Funnel: leads → qualified → won
    lead_counts = {
        "total_leads": db.query(func.count(Lead.id)).scalar() or 0,
        "qualified": db.query(func.count(Lead.id)).filter(Lead.status == LeadStatus.QUALIFIED).scalar() or 0,
        "converted": db.query(func.count(Lead.id)).filter(Lead.status == LeadStatus.CONVERTED).scalar() or 0,
        "won_deals": len(won),
    }

    # Rep leaderboard (managers only) — open pipeline, won deals (count + value), activities (30d)
    rep_rows = []
    if user.is_manager:
        reps = db.query(User).filter(User.is_active == True).all()
        # Group deals by owner
        deals_by_owner = defaultdict(list)
        for d in all_deals:
            if d.owner_id:
                deals_by_owner[d.owner_id].append(d)
        # Activity counts last 30 days
        rows = (
            db.query(Activity.created_by_id, func.count(Activity.id))
            .filter(Activity.created_at >= thirty_days_ago)
            .group_by(Activity.created_by_id)
            .all()
        )
        act_count_by_user = {row[0]: row[1] for row in rows if row[0]}
        for rep in reps:
            owned = deals_by_owner.get(rep.id, [])
            owned_open = [d for d in owned if d.stage in OPEN_STAGES]
            owned_won = [d for d in owned if d.stage == DealStage.CLOSED_WON]
            rep_rows.append({
                "rep": rep,
                "pipeline_value": sum(float(d.value or 0) for d in owned_open),
                "open_count": len(owned_open),
                "won_count": len(owned_won),
                "won_value": sum(float(d.value or 0) for d in owned_won),
                "activity_30d": act_count_by_user.get(rep.id, 0),
            })
        rep_rows.sort(key=lambda r: r["pipeline_value"], reverse=True)

    # Activity volume by type (last 30 days)
    recent_acts = acts_q.filter(Activity.created_at >= thirty_days_ago).all()
    activity_by_type = {t.value: 0 for t in ActivityType}
    for a in recent_acts:
        activity_by_type[a.type.value] = activity_by_type.get(a.type.value, 0) + 1

    return templates.TemplateResponse(request, "reports/index.html", {
        "user": user,
        "flash": get_flash(request),
        "pipeline_value": pipeline_value,
        "won_value_total": won_value_total,
        "won_value_month": won_value_month,
        "won_count": len(won),
        "lost_count": len(lost),
        "open_count": len(open_deals),
        "win_rate": win_rate,
        "by_stage": by_stage,
        "lead_counts": lead_counts,
        "rep_rows": rep_rows,
        "activity_by_type": activity_by_type,
        "activity_total_30d": len(recent_acts),
    })

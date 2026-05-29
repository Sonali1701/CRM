"""
Unified Sales Hub — single dashboard replacing fragmented Reports/Activities/Funnel.
Shows: contact status breakdown, engagement metrics, activity feed, follow-ups, team performance.
"""
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models import User, Lead, Activity
from app.models.lead import LeadStatus
from app.models.activity import ActivityType
from app.services.lead_intelligence import get_engagement_score
from app.templating import templates

router = APIRouter()


@router.get("/sales-hub")
def sales_hub(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Unified dashboard showing all sales metrics and activity in one view."""
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Scope: managers see all, reps see only their own
    leads_q = db.query(Lead)
    if not user.is_manager:
        leads_q = leads_q.filter(Lead.owner_id == user.id)

    all_leads = leads_q.all()

    # 1. Contact Status Breakdown
    status_breakdown = {
        "new": len([l for l in all_leads if l.status == LeadStatus.NEW]),
        "contacted": len([l for l in all_leads if l.status == LeadStatus.CONTACTED]),
        "qualified": len([l for l in all_leads if l.status == LeadStatus.QUALIFIED]),
        "converted": len([l for l in all_leads if l.status == LeadStatus.CONVERTED]),
        "disqualified": len([l for l in all_leads if l.status == LeadStatus.DISQUALIFIED]),
    }
    total_leads = sum(status_breakdown.values())

    # 2. Conversion Metrics
    conversion_rate = (
        status_breakdown["converted"] / (total_leads - status_breakdown["disqualified"]) * 100
        if (total_leads - status_breakdown["disqualified"]) > 0
        else 0
    )
    contacted_rate = (
        (status_breakdown["contacted"] + status_breakdown["qualified"] + status_breakdown["converted"]) / total_leads * 100
        if total_leads > 0
        else 0
    )

    # 3. Top Engaged Contacts (by engagement score)
    contacts_with_scores = [
        {"lead": l, "score": get_engagement_score(l)}
        for l in all_leads
    ]
    top_engaged = sorted(contacts_with_scores, key=lambda x: x["score"], reverse=True)[:10]

    # 4. Follow-ups Due (open TASK activities, due soon)
    follow_ups_due = (
        db.query(Activity)
        .filter(
            Activity.type == ActivityType.TASK,
            Activity.completed == False,  # noqa: E712
            Activity.due_at.isnot(None),
            Activity.due_at <= now.replace(hour=23, minute=59, second=59) + timedelta(days=7),
        )
        .order_by(Activity.due_at)
        .limit(15)
        .all()
    )
    if not user.is_manager:
        follow_ups_due = [a for a in follow_ups_due if a.created_by_id == user.id or (a.lead and a.lead.owner_id == user.id)]

    # 5. Recent Activities (last 20, any type)
    recent_activities = (
        db.query(Activity)
        .filter(Activity.created_at >= thirty_days_ago)
        .order_by(Activity.created_at.desc())
        .limit(20)
        .all()
    )
    if not user.is_manager:
        recent_activities = [a for a in recent_activities if a.created_by_id == user.id]

    # 6. Team Performance (managers only)
    team_stats = []
    if user.is_manager:
        reps = db.query(User).filter(User.is_active == True).all()
        for rep in reps:
            rep_leads = db.query(Lead).filter(Lead.owner_id == rep.id).all()
            rep_converted = len([l for l in rep_leads if l.status == LeadStatus.CONVERTED])
            rep_qualified = len([l for l in rep_leads if l.status == LeadStatus.QUALIFIED])
            rep_contacted = len([l for l in rep_leads if l.status == LeadStatus.CONTACTED])
            rep_activities = (
                db.query(func.count(Activity.id))
                .filter(Activity.created_by_id == rep.id, Activity.created_at >= thirty_days_ago)
                .scalar()
                or 0
            )
            team_stats.append({
                "rep": rep,
                "total_leads": len(rep_leads),
                "converted": rep_converted,
                "qualified": rep_qualified,
                "contacted": rep_contacted,
                "activities_30d": rep_activities,
                "conversion_rate": (rep_converted / len(rep_leads) * 100) if rep_leads else 0,
            })
        team_stats = sorted(team_stats, key=lambda x: x["converted"], reverse=True)

    return templates.TemplateResponse(request, "sales_hub.html", {
        "user": user,
        "status_breakdown": status_breakdown,
        "total_leads": total_leads,
        "conversion_rate": conversion_rate,
        "contacted_rate": contacted_rate,
        "top_engaged": top_engaged,
        "follow_ups_due": follow_ups_due,
        "recent_activities": recent_activities,
        "team_stats": team_stats,
        "now": now,
    })

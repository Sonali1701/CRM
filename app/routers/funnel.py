from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import get_flash
from app.models import User, Lead, Activity
from app.models.activity import ActivityType
from app.models.lead import LeadStatus
from app.templating import templates

router = APIRouter()

_FUNNEL_STAGES = [
    LeadStatus.NEW,
    LeadStatus.CONTACTED,
    LeadStatus.QUALIFIED,
    LeadStatus.CONVERTED,
]

_COLORS = {
    LeadStatus.NEW: "bg-slate-100 text-slate-700 border-slate-200",
    LeadStatus.CONTACTED: "bg-blue-50 text-blue-700 border-blue-200",
    LeadStatus.QUALIFIED: "bg-indigo-50 text-indigo-700 border-indigo-200",
    LeadStatus.CONVERTED: "bg-green-50 text-green-700 border-green-200",
    LeadStatus.DISQUALIFIED: "bg-red-50 text-red-700 border-red-200",
}

_BAR_COLORS = {
    LeadStatus.NEW: "bg-slate-400",
    LeadStatus.CONTACTED: "bg-blue-400",
    LeadStatus.QUALIFIED: "bg-indigo-500",
    LeadStatus.CONVERTED: "bg-green-500",
    LeadStatus.DISQUALIFIED: "bg-red-400",
}


@router.get("/funnel")
def funnel_view(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = db.query(Lead)
    if not user.is_manager:
        q = q.filter(Lead.owner_id == user.id)
    leads = q.all()

    by_status: dict[LeadStatus, list] = {s: [] for s in LeadStatus}
    for lead in leads:
        by_status[lead.status].append(lead)

    total = len(leads)
    top_count = len(by_status[LeadStatus.NEW]) or 1

    stages = []
    for status in _FUNNEL_STAGES:
        bucket = by_status[status]
        count = len(bucket)
        pct_total = round(count / total * 100) if total else 0
        pct_bar = round(count / top_count * 100) if top_count else 0
        stages.append({
            "status": status,
            "label": status.value.replace("_", " ").title(),
            "leads": bucket[:10],
            "count": count,
            "pct_total": pct_total,
            "pct_bar": pct_bar,
            "color": _COLORS[status],
            "bar_color": _BAR_COLORS[status],
        })

    disqualified = by_status[LeadStatus.DISQUALIFIED]

    # Upcoming outreach reminders (open tasks linked to leads)
    now = datetime.now(timezone.utc)
    reminder_q = (
        db.query(Activity)
        .filter(
            Activity.type == ActivityType.TASK,
            Activity.completed == False,
            Activity.lead_id.isnot(None),
            Activity.due_at.isnot(None),
        )
        .order_by(Activity.due_at)
    )
    if not user.is_manager:
        reminder_q = reminder_q.filter(Activity.created_by_id == user.id)
    reminders = reminder_q.limit(20).all()

    # Attach lead names
    lead_map = {l.id: l for l in leads}

    return templates.TemplateResponse(request, "funnel.html", {
        "user": user,
        "flash": get_flash(request),
        "stages": stages,
        "total": total,
        "disqualified": disqualified,
        "reminders": reminders,
        "lead_map": lead_map,
        "now": now,
    })

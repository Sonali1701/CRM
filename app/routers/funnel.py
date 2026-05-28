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

# SVG fill hex — dark-to-light blue gradient (navy → mid-blue → sky → cyan)
_FILL_HEX = {
    LeadStatus.NEW: "#0c2340",
    LeadStatus.CONTACTED: "#1a56db",
    LeadStatus.QUALIFIED: "#3f83f8",
    LeadStatus.CONVERTED: "#0ea5e9",
}

# Fixed funnel widths (px out of 600) for each stage — always decreasing
# so the shape looks like a proper inverted triangle regardless of data.
_FUNNEL_WIDTHS = [556, 424, 292, 160]


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

    stages = []
    for status in _FUNNEL_STAGES:
        bucket = by_status[status]
        count = len(bucket)
        idx = len(stages)
        stages.append({
            "status": status,
            "label": status.value.replace("_", " ").title(),
            "leads": bucket[:8],
            "count": count,
            "pct_total": round(count / total * 100) if total else 0,
            "top_w": _FUNNEL_WIDTHS[idx],
            "bot_w": _FUNNEL_WIDTHS[idx + 1] if idx + 1 < len(_FUNNEL_WIDTHS) else 60,
            "fill": _FILL_HEX[status],
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

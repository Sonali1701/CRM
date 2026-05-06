from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import require_user
from app.flash import get_flash
from app.models import User, Deal
from app.models.deal import OPEN_STAGES, STAGE_LABELS
from app.templating import templates

router = APIRouter()


@router.get("/pipeline")
def pipeline_board(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = db.query(Deal).filter(Deal.stage.in_(OPEN_STAGES))
    if not user.is_manager:
        q = q.filter(Deal.owner_id == user.id)
    deals = q.order_by(Deal.stage_position, Deal.created_at).all()

    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().stale_deal_days)

    columns = {}
    for stage in OPEN_STAGES:
        stage_deals = [d for d in deals if d.stage == stage]
        columns[stage] = {
            "label": STAGE_LABELS[stage],
            "deals": stage_deals,
            "total": sum(float(d.value or 0) for d in stage_deals),
        }

    def is_stale(deal):
        ref = deal.last_activity_at or deal.updated_at
        if ref and ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return ref is None or ref < stale_cutoff

    return templates.TemplateResponse(request, "pipeline.html", {
        "user": user, "flash": get_flash(request),
        "columns": columns, "OPEN_STAGES": OPEN_STAGES,
        "is_stale": is_stale,
    })

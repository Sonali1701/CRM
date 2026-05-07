from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, Deal, Client, Activity
from app.models.deal import DealStage, OPEN_STAGES, PIPELINE_STAGES, STAGE_LABELS
from app.templating import templates

router = APIRouter()


def _deal_query(db: Session, user: User):
    q = db.query(Deal)
    if not user.is_manager:
        q = q.filter(Deal.owner_id == user.id)
    return q


@router.get("")
def deals_list(request: Request, stage: str = "", search: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = _deal_query(db, user)
    if stage:
        q = q.filter(Deal.stage == stage)
    if search:
        q = q.filter(Deal.title.ilike(f"%{search}%"))
    deals = q.order_by(Deal.updated_at.desc()).all()
    clients = db.query(Client).order_by(Client.name).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    return templates.TemplateResponse(request, "deals/list.html", {
        "user": user, "flash": get_flash(request),
        "deals": deals, "clients": clients, "reps": reps,
        "stages": PIPELINE_STAGES, "stage_labels": STAGE_LABELS,
        "filter_stage": stage, "search": search,
    })


@router.get("/new")
def deal_new(request: Request, client_id: int = None, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clients = db.query(Client).order_by(Client.name).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    return templates.TemplateResponse(request, "deals/form.html", {
        "user": user, "deal": None, "clients": clients, "reps": reps,
        "stages": OPEN_STAGES, "stage_labels": STAGE_LABELS,
        "prefill_client_id": client_id,
    })


@router.post("/new")
def deal_create(
    request: Request,
    title: str = Form(...), client_id: str = Form(""), owner_id: str = Form(""),
    value: float = Form(0), stage: str = Form("lead_generated"),
    expected_close_date: str = Form(""), probability: int = Form(0),
    notes: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    cid = int(client_id) if client_id.strip().isdigit() else None
    oid = int(owner_id) if owner_id.strip().isdigit() else None
    deal = Deal(
        title=title, client_id=cid,
        owner_id=oid if user.is_manager else user.id,
        value=value, stage=DealStage(stage),
        expected_close_date=datetime.strptime(expected_close_date, "%Y-%m-%d").date() if expected_close_date else None,
        probability=probability, notes=notes or None,
    )
    db.add(deal); db.commit()
    return flash(RedirectResponse(f"/deals/{deal.id}", 303), "Deal created.")


@router.get("/{deal_id}")
def deal_detail(deal_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deal = _get_deal(deal_id, user, db)
    clients = db.query(Client).order_by(Client.name).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    activities = db.query(Activity).filter(Activity.deal_id == deal_id).order_by(Activity.created_at.desc()).all()
    from app.models.activity import ActivityType
    return templates.TemplateResponse(request, "deals/detail.html", {
        "user": user, "flash": get_flash(request),
        "deal": deal, "clients": clients, "reps": reps, "activities": activities,
        "stages": PIPELINE_STAGES, "stage_labels": STAGE_LABELS,
        "ActivityType": ActivityType,
    })


@router.post("/{deal_id}")
def deal_update(
    deal_id: int,
    title: str = Form(...), client_id: str = Form(""), owner_id: str = Form(""),
    value: float = Form(0), stage: str = Form(...),
    expected_close_date: str = Form(""), probability: int = Form(0),
    notes: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    deal = _get_deal(deal_id, user, db)
    deal.title = title
    deal.client_id = int(client_id) if client_id.strip().isdigit() else None
    deal.value = value; deal.stage = DealStage(stage)
    deal.expected_close_date = datetime.strptime(expected_close_date, "%Y-%m-%d").date() if expected_close_date else None
    deal.probability = probability; deal.notes = notes or None
    if user.is_manager:
        deal.owner_id = int(owner_id) if owner_id.strip().isdigit() else None
    db.commit()
    return flash(RedirectResponse(f"/deals/{deal_id}", 303), "Deal updated.")


@router.post("/{deal_id}/stage")
async def deal_move_stage(deal_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """HTMX/JSON endpoint for Kanban drag-drop stage change."""
    data = await request.json()
    new_stage = data.get("stage")
    if new_stage not in [s.value for s in DealStage]:
        raise HTTPException(400, "Invalid stage")
    deal = _get_deal(deal_id, user, db)
    deal.stage = DealStage(new_stage)
    db.commit()
    return JSONResponse({"ok": True, "stage": new_stage, "label": STAGE_LABELS[deal.stage]})


@router.post("/{deal_id}/delete")
def deal_delete(deal_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deal = _get_deal(deal_id, user, db)
    db.delete(deal); db.commit()
    return flash(RedirectResponse("/deals", 303), "Deal deleted.", "error")


def _get_deal(deal_id: int, user: User, db: Session) -> Deal:
    q = db.query(Deal).filter(Deal.id == deal_id)
    if not user.is_manager:
        q = q.filter(Deal.owner_id == user.id)
    deal = q.first()
    if not deal:
        raise HTTPException(404, "Deal not found")
    return deal

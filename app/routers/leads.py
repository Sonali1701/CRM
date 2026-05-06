from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, Lead, Client
from app.models.lead import LeadStatus
from app.templating import templates

router = APIRouter()


def _leads_query(db: Session, user: User):
    q = db.query(Lead)
    if not user.is_manager:
        q = q.filter(Lead.owner_id == user.id)
    return q


@router.get("")
def leads_list(request: Request, status: str = "", search: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = _leads_query(db, user)
    if status:
        q = q.filter(Lead.status == status)
    if search:
        like = f"%{search}%"
        q = q.filter((Lead.name.ilike(like)) | (Lead.company.ilike(like)) | (Lead.email.ilike(like)))
    leads = q.order_by(Lead.created_at.desc()).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    return templates.TemplateResponse(request, "leads/list.html", {
        "user": user, "flash": get_flash(request),
        "leads": leads, "reps": reps,
        "statuses": list(LeadStatus), "filter_status": status, "search": search,
    })


@router.get("/new")
def lead_new(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    return templates.TemplateResponse(request, "leads/form.html", {
        "user": user, "lead": None, "reps": reps, "statuses": list(LeadStatus),
    })


@router.post("/new")
def lead_create(
    request: Request,
    name: str = Form(...), company: str = Form(""), email: str = Form(""),
    phone: str = Form(""), source: str = Form(""), notes: str = Form(""),
    owner_id: int = Form(None),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    lead = Lead(
        name=name, company=company or None, email=email or None,
        phone=phone or None, source=source or None, notes=notes or None,
        owner_id=owner_id if user.is_manager else user.id,
        status=LeadStatus.NEW,
    )
    db.add(lead); db.commit()
    return flash(RedirectResponse("/leads", 303), "Lead created.")


@router.get("/{lead_id}")
def lead_detail(lead_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lead = _get_lead(lead_id, user, db)
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    return templates.TemplateResponse(request, "leads/form.html", {
        "user": user, "flash": get_flash(request),
        "lead": lead, "reps": reps, "statuses": list(LeadStatus),
    })


@router.post("/{lead_id}")
def lead_update(
    lead_id: int, request: Request,
    name: str = Form(...), company: str = Form(""), email: str = Form(""),
    phone: str = Form(""), source: str = Form(""), notes: str = Form(""),
    status: str = Form(...), owner_id: int = Form(None),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    lead = _get_lead(lead_id, user, db)
    lead.name = name; lead.company = company or None; lead.email = email or None
    lead.phone = phone or None; lead.source = source or None; lead.notes = notes or None
    lead.status = LeadStatus(status)
    if user.is_manager and owner_id:
        lead.owner_id = owner_id
    db.commit()
    return flash(RedirectResponse("/leads", 303), "Lead updated.")


@router.post("/{lead_id}/convert")
def lead_convert(lead_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lead = _get_lead(lead_id, user, db)
    client = Client(
        name=lead.company or lead.name,
        owner_id=lead.owner_id or user.id,
    )
    db.add(client); db.flush()
    lead.status = LeadStatus.CONVERTED
    lead.converted_client_id = client.id
    db.commit()
    return flash(RedirectResponse(f"/clients/{client.id}", 303), f"Lead converted to client: {client.name}")


@router.post("/{lead_id}/delete")
def lead_delete(lead_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lead = _get_lead(lead_id, user, db)
    db.delete(lead); db.commit()
    return flash(RedirectResponse("/leads", 303), "Lead deleted.", "error")


def _get_lead(lead_id: int, user: User, db: Session) -> Lead:
    q = db.query(Lead).filter(Lead.id == lead_id)
    if not user.is_manager:
        q = q.filter(Lead.owner_id == user.id)
    lead = q.first()
    if not lead:
        from fastapi import HTTPException
        raise HTTPException(404, "Lead not found")
    return lead

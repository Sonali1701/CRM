from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
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


def _resolve_client_id(db: Session, explicit_id: int | None, company_name: str | None) -> tuple[int | None, str | None]:
    """Return (client_id, resolved_company_name). If explicit_id is set, use it.
    Otherwise, match a Client whose name equals the company text (case-insensitive)."""
    if explicit_id:
        c = db.get(Client, explicit_id)
        if c:
            return c.id, c.name
        return None, company_name
    if company_name:
        match = db.query(Client).filter(Client.name.ilike(company_name.strip())).first()
        if match:
            return match.id, match.name
    return None, company_name


@router.get("")
def leads_list(request: Request, status: str = "", search: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = _leads_query(db, user)
    if status:
        q = q.filter(Lead.status == status)
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(Lead.first_name.ilike(like), Lead.last_name.ilike(like),
                Lead.company.ilike(like), Lead.email.ilike(like))
        )
    leads = q.order_by(Lead.created_at.desc()).all()
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    # Templates + sequences available to this user for the bulk-mail modal
    from app.models import EmailTemplate, EmailSequence
    email_templates = (
        db.query(EmailTemplate)
        .filter(or_(EmailTemplate.created_by_id == user.id, EmailTemplate.is_shared == True))
        .order_by(EmailTemplate.name)
        .all()
    )
    sequences = db.query(EmailSequence).filter(EmailSequence.is_active == True).order_by(EmailSequence.name).all()
    return templates.TemplateResponse(request, "leads/list.html", {
        "user": user, "flash": get_flash(request),
        "leads": leads, "reps": reps,
        "statuses": list(LeadStatus), "filter_status": status, "search": search,
        "email_templates": email_templates, "sequences": sequences,
    })


@router.post("/delete-all")
def leads_delete_all(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Admin-only nuke: delete every contact. Useful after a bad bulk import."""
    if not user.is_admin:
        from fastapi import HTTPException
        raise HTTPException(403, "Admin only")
    count = db.query(Lead).delete()
    db.commit()
    return flash(RedirectResponse("/leads", 303), f"Deleted {count} contacts.", "error")


@router.get("/new")
def lead_new(request: Request, client_id: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    clients = db.query(Client).order_by(Client.name).all()
    preset_client_id = int(client_id) if client_id.strip().isdigit() else None
    return templates.TemplateResponse(request, "leads/form.html", {
        "user": user, "lead": None, "reps": reps, "statuses": list(LeadStatus),
        "clients": clients, "preset_client_id": preset_client_id,
    })


@router.post("/new")
def lead_create(
    request: Request,
    first_name: str = Form(...), last_name: str = Form(""),
    job_title: str = Form(""), company: str = Form(""),
    client_id: str = Form(""),
    email: str = Form(""), mobile: str = Form(""), phone: str = Form(""),
    linkedin_url: str = Form(""),
    city: str = Form(""), state: str = Form(""), country: str = Form(""),
    source: str = Form(""), notes: str = Form(""),
    owner_id: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    owner = int(owner_id) if owner_id.strip().isdigit() else None
    explicit_cid = int(client_id) if client_id.strip().isdigit() else None
    cid, resolved_company = _resolve_client_id(db, explicit_cid, company or None)
    lead = Lead(
        first_name=first_name, last_name=last_name or None,
        job_title=job_title or None, company=resolved_company,
        client_id=cid,
        email=email or None, mobile=mobile or None, phone=phone or None,
        linkedin_url=linkedin_url or None,
        city=city or None, state=state or None, country=country or None,
        source=source or None, notes=notes or None,
        owner_id=owner if user.is_manager else user.id,
        status=LeadStatus.NEW,
    )
    db.add(lead)
    db.commit()
    return flash(RedirectResponse("/leads", 303), "Contact created.")


@router.get("/{lead_id}")
def lead_detail(lead_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lead = _get_lead(lead_id, user, db)
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    clients = db.query(Client).order_by(Client.name).all()
    return templates.TemplateResponse(request, "leads/form.html", {
        "user": user, "flash": get_flash(request),
        "lead": lead, "reps": reps, "statuses": list(LeadStatus), "clients": clients,
    })


@router.post("/{lead_id}")
def lead_update(
    lead_id: int, request: Request,
    first_name: str = Form(...), last_name: str = Form(""),
    job_title: str = Form(""), company: str = Form(""),
    client_id: str = Form(""),
    email: str = Form(""), mobile: str = Form(""), phone: str = Form(""),
    linkedin_url: str = Form(""),
    city: str = Form(""), state: str = Form(""), country: str = Form(""),
    source: str = Form(""), notes: str = Form(""),
    status: str = Form(...), owner_id: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    lead = _get_lead(lead_id, user, db)
    explicit_cid = int(client_id) if client_id.strip().isdigit() else None
    cid, resolved_company = _resolve_client_id(db, explicit_cid, company or None)
    lead.first_name = first_name
    lead.last_name = last_name or None
    lead.job_title = job_title or None
    lead.company = resolved_company
    lead.client_id = cid
    lead.email = email or None
    lead.mobile = mobile or None
    lead.phone = phone or None
    lead.linkedin_url = linkedin_url or None
    lead.city = city or None
    lead.state = state or None
    lead.country = country or None
    lead.source = source or None
    lead.notes = notes or None
    lead.status = LeadStatus(status)
    if user.is_manager:
        lead.owner_id = int(owner_id) if owner_id.strip().isdigit() else None
    db.commit()
    return flash(RedirectResponse("/leads", 303), "Contact updated.")


@router.post("/{lead_id}/convert")
def lead_convert(lead_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lead = _get_lead(lead_id, user, db)
    client = Client(
        name=lead.company or lead.name,
        owner_id=lead.owner_id or user.id,
    )
    db.add(client)
    db.flush()
    lead.status = LeadStatus.CONVERTED
    lead.converted_client_id = client.id
    db.commit()
    return flash(RedirectResponse(f"/clients/{client.id}", 303), f"Contact converted to company: {client.name}")


@router.post("/{lead_id}/delete")
def lead_delete(lead_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    lead = _get_lead(lead_id, user, db)
    db.delete(lead)
    db.commit()
    return flash(RedirectResponse("/leads", 303), "Contact deleted.", "error")


@router.post("/{lead_id}/assign")
async def lead_assign(
    lead_id: int, request: Request,
    owner_id: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    if not user.is_manager:
        from fastapi import HTTPException
        raise HTTPException(403)
    lead = _get_lead_any(lead_id, db)
    lead.owner_id = int(owner_id) if owner_id.strip().isdigit() else None
    db.commit()
    reps = db.query(User).filter(User.is_active == True).all()
    return templates.TemplateResponse(request, "leads/_row.html", {
        "lead": lead, "user": user, "reps": reps,
    })


def _get_lead_any(lead_id: int, db: Session) -> Lead:
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        from fastapi import HTTPException
        raise HTTPException(404, "Contact not found")
    return lead


def _get_lead(lead_id: int, user: User, db: Session) -> Lead:
    q = db.query(Lead).filter(Lead.id == lead_id)
    if not user.is_manager:
        q = q.filter(Lead.owner_id == user.id)
    lead = q.first()
    if not lead:
        from fastapi import HTTPException
        raise HTTPException(404, "Contact not found")
    return lead

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, Client, Contact, Deal, Activity
from app.models.client import ClientType
from app.services.enrich import enrich
from app.templating import templates

router = APIRouter()


def _client_query(db: Session, user: User):
    q = db.query(Client)
    if not user.is_manager:
        q = q.filter(Client.owner_id == user.id)
    return q


@router.get("")
def clients_list(request: Request, search: str = "", type_: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = _client_query(db, user)
    if search:
        q = q.filter(Client.name.ilike(f"%{search}%"))
    if type_:
        q = q.filter(Client.type == type_)
    clients = q.order_by(Client.name).all()
    return templates.TemplateResponse(request, "clients/list.html", {
        "user": user, "flash": get_flash(request),
        "clients": clients, "types": list(ClientType),
        "filter_type": type_, "search": search,
    })


@router.get("/new")
def client_new(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    return templates.TemplateResponse(request, "clients/form.html", {
        "user": user, "client": None, "types": list(ClientType), "reps": reps,
    })


@router.post("/new")
def client_create(
    request: Request,
    name: str = Form(...), type_: str = Form("direct"), website: str = Form(""),
    industry: str = Form(""), notes: str = Form(""), owner_id: int = Form(None),
    hq_city: str = Form(""), hq_state: str = Form(""), hq_country: str = Form(""),
    phone: str = Form(""), email: str = Form(""), linkedin_url: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    client = Client(
        name=name, type=ClientType(type_), website=website or None,
        industry=industry or None, notes=notes or None,
        owner_id=owner_id if user.is_manager else user.id,
        hq_city=hq_city or None, hq_state=hq_state or None, hq_country=hq_country or None,
        phone=phone or None, email=email or None, linkedin_url=linkedin_url or None,
    )
    db.add(client)
    db.commit()
    return flash(RedirectResponse(f"/clients/{client.id}", 303), "Company created.")


@router.get("/{client_id}")
def client_detail(client_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    client = _get_client(client_id, user, db)
    reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
    deals = db.query(Deal).filter(Deal.client_id == client_id).order_by(Deal.created_at.desc()).all()
    deal_ids = [d.id for d in deals]
    activities = db.query(Activity).filter(
        or_(Activity.client_id == client_id, Activity.deal_id.in_(deal_ids) if deal_ids else False)
    ).order_by(Activity.created_at.desc()).limit(50).all()
    from app.models.activity import ActivityType
    return templates.TemplateResponse(request, "clients/detail.html", {
        "user": user, "flash": get_flash(request),
        "client": client, "types": list(ClientType), "reps": reps,
        "deals": deals, "activities": activities,
        "activity_types": list(ActivityType),
    })


@router.post("/{client_id}")
def client_update(
    client_id: int,
    name: str = Form(...), type_: str = Form("direct"), website: str = Form(""),
    industry: str = Form(""), notes: str = Form(""), owner_id: int = Form(None),
    hq_city: str = Form(""), hq_state: str = Form(""), hq_country: str = Form(""),
    phone: str = Form(""), email: str = Form(""), linkedin_url: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    client = _get_client(client_id, user, db)
    client.name = name
    client.type = ClientType(type_)
    client.website = website or None
    client.industry = industry or None
    client.notes = notes or None
    client.hq_city = hq_city or None
    client.hq_state = hq_state or None
    client.hq_country = hq_country or None
    client.phone = phone or None
    client.email = email or None
    client.linkedin_url = linkedin_url or None
    if user.is_manager and owner_id:
        client.owner_id = owner_id
    db.commit()
    return flash(RedirectResponse(f"/clients/{client_id}", 303), "Company updated.")


@router.post("/{client_id}/contacts/add")
def contact_add(
    client_id: int,
    full_name: str = Form(...), title: str = Form(""), email: str = Form(""),
    phone: str = Form(""), is_primary: bool = Form(False),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    _get_client(client_id, user, db)
    contact = Contact(
        client_id=client_id, full_name=full_name, title=title or None,
        email=email or None, phone=phone or None, is_primary=is_primary,
    )
    db.add(contact)
    db.commit()
    return flash(RedirectResponse(f"/clients/{client_id}", 303), "Contact added.")


@router.post("/{client_id}/contacts/{contact_id}/delete")
def contact_delete(
    client_id: int, contact_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    _get_client(client_id, user, db)
    contact = db.get(Contact, contact_id)
    if contact and contact.client_id == client_id:
        db.delete(contact)
        db.commit()
    return flash(RedirectResponse(f"/clients/{client_id}", 303), "Contact removed.", "error")


@router.post("/{client_id}/delete")
def client_delete(client_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    client = _get_client(client_id, user, db)
    db.delete(client)
    db.commit()
    return flash(RedirectResponse("/clients", 303), "Company deleted.", "error")


@router.get("/enrich")
async def client_enrich(url: str, user: User = Depends(require_user)):
    if not url:
        return JSONResponse({"error": "No URL provided"}, status_code=400)
    data = await enrich(url)
    return JSONResponse(data)


def _get_client(client_id: int, user: User, db: Session) -> Client:
    q = db.query(Client).filter(Client.id == client_id)
    if not user.is_manager:
        q = q.filter(Client.owner_id == user.id)
    client = q.first()
    if not client:
        raise HTTPException(404, "Company not found")
    return client

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, Client, Contact, Deal, Activity, Lead, EmailMessage, AccountPlan
from app.models.client import ClientType
from app.models.deal import OPEN_STAGES, DealStage
from app.services.enrich import enrich
from app.templating import templates

router = APIRouter()


def _client_query(db: Session, user: User):
    q = db.query(Client)
    if not user.is_manager:
        q = q.filter(Client.owner_id == user.id)
    return q


def _compute_opportunity_scores(db: Session, clients: list[Client]) -> dict[int, dict]:
    """Lightweight opportunity score per client (0-100), with the inputs that
    drove it so we can show them in tooltips.

    Inputs:
      - Open pipeline value (log-scaled, max 40 points)
      - Open deal count (max 15)
      - Recent activity count last 30d (max 25)
      - Stage progression: weighted by furthest open stage (max 20)
    """
    if not clients:
        return {}
    cids = [c.id for c in clients]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    stage_weight = {
        DealStage.LEAD_GENERATED: 1,
        DealStage.QUALIFIED: 2,
        DealStage.DISCOVERY_DONE: 3,
        DealStage.REQUIREMENT_RECEIVED: 4,
        DealStage.PROPOSAL_SHARED: 5,
        DealStage.NEGOTIATION: 6,
    }

    deals = db.query(Deal).filter(Deal.client_id.in_(cids)).all()
    activity_counts = dict(
        db.query(Activity.client_id, func.count(Activity.id))
        .filter(Activity.client_id.in_(cids), Activity.created_at >= cutoff)
        .group_by(Activity.client_id)
        .all()
    )

    out: dict[int, dict] = {}
    for c in clients:
        cdeals = [d for d in deals if d.client_id == c.id]
        open_deals = [d for d in cdeals if d.stage in OPEN_STAGES]
        open_value = sum(float(d.value or 0) for d in open_deals)
        open_count = len(open_deals)
        act_30d = activity_counts.get(c.id, 0)
        max_stage = max((stage_weight.get(d.stage, 0) for d in open_deals), default=0)

        # Log-scaled value: $10k → ~10, $100k → ~20, $1M → ~30, $10M → ~40
        from math import log10
        value_score = min(40, log10(open_value + 1) * 8) if open_value > 0 else 0
        count_score = min(15, open_count * 5)
        act_score = min(25, act_30d * 3)
        stage_score = min(20, max_stage * 3.5)
        score = int(round(value_score + count_score + act_score + stage_score))

        out[c.id] = {
            "score": score,
            "open_value": open_value,
            "open_count": open_count,
            "act_30d": act_30d,
            "max_stage": max_stage,
        }
    return out


def _compute_penetration(db: Session, client: Client) -> dict:
    """Account-penetration metrics for the client detail page."""
    now = datetime.now(timezone.utc)
    deals = db.query(Deal).filter(Deal.client_id == client.id).all()
    open_deals = [d for d in deals if d.stage in OPEN_STAGES]
    won = [d for d in deals if d.stage == DealStage.CLOSED_WON]
    lost = [d for d in deals if d.stage == DealStage.CLOSED_LOST]
    closed = len(won) + len(lost)
    open_value = sum(float(d.value or 0) for d in open_deals)
    won_value = sum(float(d.value or 0) for d in won)

    contact_count = db.query(func.count(Lead.id)).filter(Lead.client_id == client.id).scalar() or 0
    qualified_contact_count = (
        db.query(func.count(Lead.id))
        .filter(Lead.client_id == client.id, Lead.status.in_(["qualified", "converted"]))
        .scalar() or 0
    )

    last_act = (
        db.query(func.max(Activity.created_at))
        .filter(Activity.client_id == client.id)
        .scalar()
    )
    days_quiet = None
    if last_act:
        if last_act.tzinfo is None:
            last_act = last_act.replace(tzinfo=timezone.utc)
        days_quiet = (now - last_act).days

    thirty_days_ago = now - timedelta(days=30)
    acts_30d = (
        db.query(func.count(Activity.id))
        .filter(Activity.client_id == client.id, Activity.created_at >= thirty_days_ago)
        .scalar() or 0
    )

    return {
        "open_count": len(open_deals),
        "open_value": open_value,
        "won_count": len(won),
        "won_value": won_value,
        "lost_count": len(lost),
        "win_rate": (len(won) / closed * 100) if closed else None,
        "contact_count": contact_count,
        "qualified_contact_count": qualified_contact_count,
        "last_activity_at": last_act,
        "days_quiet": days_quiet,
        "acts_30d": acts_30d,
    }


@router.get("")
def clients_list(request: Request, search: str = "", type_: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = _client_query(db, user)
    if search:
        q = q.filter(Client.name.ilike(f"%{search}%"))
    if type_:
        q = q.filter(Client.type == type_)
    clients = q.order_by(Client.name).all()
    scores = _compute_opportunity_scores(db, clients)
    # Sort by score descending so highest-opportunity accounts surface
    clients = sorted(clients, key=lambda c: -(scores.get(c.id, {}).get("score", 0)))
    return templates.TemplateResponse(request, "clients/list.html", {
        "user": user, "flash": get_flash(request),
        "clients": clients, "types": list(ClientType),
        "filter_type": type_, "search": search,
        "scores": scores,
    })


@router.get("/enrich")
async def client_enrich(url: str = "", user: User = Depends(require_user)):
    """Fetch company details from a website URL — called via fetch() from the form."""
    if not url.strip():
        return JSONResponse({"error": "No URL provided"}, status_code=400)
    data = await enrich(url)
    return JSONResponse(data)


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
    industry: str = Form(""), description: str = Form(""), notes: str = Form(""),
    owner_id: str = Form(""),
    hq_city: str = Form(""), hq_state: str = Form(""), hq_country: str = Form(""),
    phone: str = Form(""), email: str = Form(""), linkedin_url: str = Form(""),
    force_create: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    if not force_create:
        dup = find_client_duplicate(db, name, website)
        if dup:
            reps = db.query(User).filter(User.is_active == True).all() if user.is_manager else []
            match_basis = "name" if dup.name.lower() == name.strip().lower() else "website domain"
            return templates.TemplateResponse(request, "clients/form.html", {
                "user": user, "client": None, "types": list(ClientType), "reps": reps,
                "form_values": {
                    "name": name, "type_": type_, "website": website,
                    "industry": industry, "description": description, "notes": notes,
                    "owner_id": owner_id, "hq_city": hq_city, "hq_state": hq_state,
                    "hq_country": hq_country, "phone": phone, "email": email,
                    "linkedin_url": linkedin_url,
                },
                "duplicate": {"client": dup, "match_basis": match_basis},
            })

    owner = int(owner_id) if owner_id.strip().isdigit() else None
    client = Client(
        name=name, type=ClientType(type_), website=website or None,
        industry=industry or None, description=description or None,
        notes=notes or None,
        owner_id=owner if user.is_manager else user.id,
        hq_city=hq_city or None, hq_state=hq_state or None, hq_country=hq_country or None,
        phone=phone or None, email=email or None, linkedin_url=linkedin_url or None,
    )
    db.add(client)
    db.flush()
    # Retroactively link any unlinked leads whose company text matches this name
    db.query(Lead).filter(
        Lead.client_id.is_(None),
        Lead.company.ilike(client.name),
    ).update({"client_id": client.id}, synchronize_session=False)
    if client.owner_id:
        from app.services.notify import notify_assignment
        notify_assignment(db, assignee_id=client.owner_id, assigned_by_id=user.id,
                          entity_type="company", entity_name=client.name, link=f"/clients/{client.id}")
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
    linked_leads = (
        db.query(Lead)
        .filter(or_(
            Lead.client_id == client_id,
            and_(Lead.client_id.is_(None), Lead.company.ilike(client.name)),
        ))
        .order_by(Lead.first_name)
        .all()
    )
    from app.models.activity import ActivityType
    penetration = _compute_penetration(db, client)
    email_ids = [a.id for a in activities if a.type == ActivityType.EMAIL]
    inbound_ids = set()
    if email_ids:
        inbound_ids = {
            r[0] for r in db.query(EmailMessage.activity_id)
            .filter(EmailMessage.activity_id.in_(email_ids), EmailMessage.is_inbound == True)
            .all()
        }
    account_plan = db.query(AccountPlan).filter_by(client_id=client_id).first()
    return templates.TemplateResponse(request, "clients/detail.html", {
        "user": user, "flash": get_flash(request),
        "client": client, "types": list(ClientType), "reps": reps,
        "deals": deals, "activities": activities,
        "activity_types": list(ActivityType),
        "linked_leads": linked_leads,
        "penetration": penetration,
        "inbound_ids": inbound_ids,
        "account_plan": account_plan,
    })


@router.post("/{client_id}")
def client_update(
    client_id: int,
    name: str = Form(...), type_: str = Form("direct"), website: str = Form(""),
    industry: str = Form(""), description: str = Form(""), notes: str = Form(""),
    owner_id: str = Form(""),
    hq_city: str = Form(""), hq_state: str = Form(""), hq_country: str = Form(""),
    phone: str = Form(""), email: str = Form(""), linkedin_url: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    client = _get_client(client_id, user, db)
    client.name = name
    client.type = ClientType(type_)
    client.website = website or None
    client.industry = industry or None
    client.description = description or None
    client.notes = notes or None
    client.hq_city = hq_city or None
    client.hq_state = hq_state or None
    client.hq_country = hq_country or None
    client.phone = phone or None
    client.email = email or None
    client.linkedin_url = linkedin_url or None
    if user.is_manager:
        prev_owner = client.owner_id
        new_owner = int(owner_id) if owner_id.strip().isdigit() else None
        client.owner_id = new_owner
        if new_owner and new_owner != prev_owner:
            from app.services.notify import notify_assignment
            notify_assignment(db, assignee_id=new_owner, assigned_by_id=user.id,
                              entity_type="company", entity_name=client.name, link=f"/clients/{client_id}")
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


def _get_client(client_id: int, user: User, db: Session) -> Client:
    q = db.query(Client).filter(Client.id == client_id)
    if not user.is_manager:
        q = q.filter(Client.owner_id == user.id)
    client = q.first()
    if not client:
        raise HTTPException(404, "Company not found")
    return client


def find_client_duplicate(
    db: Session, name: str | None, website: str | None,
    exclude_id: int | None = None,
) -> Client | None:
    """Look for an existing company that's almost certainly the same one.
    Match priority:
      1. Same name (case-insensitive, trimmed)
      2. Same website domain (strips http(s):// and trailing slashes)"""
    nm = (name or "").strip()
    if nm:
        q = db.query(Client).filter(Client.name.ilike(nm))
        if exclude_id:
            q = q.filter(Client.id != exclude_id)
        match = q.first()
        if match:
            return match
    ws = (website or "").strip().lower()
    if ws:
        for prefix in ("https://", "http://"):
            if ws.startswith(prefix):
                ws = ws[len(prefix):]
        ws = ws.rstrip("/").split("/")[0]
        if ws:
            q = db.query(Client).filter(Client.website.ilike(f"%{ws}%"))
            if exclude_id:
                q = q.filter(Client.id != exclude_id)
            match = q.first()
            if match:
                return match
    return None


# ─── Account Plan ────────────────────────────────────────────────────────────

def _get_or_create_account_plan(db: Session, client: Client, user_id: int) -> AccountPlan:
    plan = db.query(AccountPlan).filter_by(client_id=client.id).first()
    if plan:
        return plan
    plan = AccountPlan(client_id=client.id, created_by_id=user_id)
    db.add(plan)
    db.flush()
    return plan


@router.post("/{client_id}/account-plan")
def account_plan_save(
    client_id: int,
    business_goals: str = Form(""),
    whitespace: str = Form(""),
    key_stakeholders: str = Form(""),
    threats_risks: str = Form(""),
    next_90d_actions: str = Form(""),
    success_metrics: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    client = _get_client(client_id, user, db)
    plan = _get_or_create_account_plan(db, client, user.id)
    plan.business_goals = business_goals or None
    plan.whitespace = whitespace or None
    plan.key_stakeholders = key_stakeholders or None
    plan.threats_risks = threats_risks or None
    plan.next_90d_actions = next_90d_actions or None
    plan.success_metrics = success_metrics or None
    db.commit()
    return flash(RedirectResponse(f"/clients/{client_id}#account-plan", 303), "Account plan saved.")


@router.post("/{client_id}/account-plan/ai-draft")
async def account_plan_ai_draft(
    client_id: int,
    overwrite: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Generate an initial draft from client + deals + activities. By default
    only fills sections that are empty — pass overwrite=1 to replace existing."""
    from app.services.ai_compose import is_ai_configured, draft_account_plan
    if not is_ai_configured():
        raise HTTPException(400, "AI not configured")
    client = _get_client(client_id, user, db)

    # Build a context block: client facts + open deals + recent activity
    lines = [f"# Account: {client.name}"]
    if client.industry: lines.append(f"Industry: {client.industry}")
    if client.description: lines.append(f"Description: {client.description}")
    if client.notes: lines.append(f"Internal notes: {client.notes}")
    open_deals = (
        db.query(Deal).filter(Deal.client_id == client.id, Deal.stage.in_(OPEN_STAGES))
        .order_by(Deal.updated_at.desc()).limit(10).all()
    )
    if open_deals:
        lines.append("\n## Open deals")
        for d in open_deals:
            lines.append(f"- {d.title} · {d.stage_label} · {d.value} {d.currency}")
    won = db.query(Deal).filter(Deal.client_id == client.id, Deal.stage == DealStage.CLOSED_WON).all()
    if won:
        lines.append(f"\n## Won deals to date: {len(won)} totalling {sum(float(d.value or 0) for d in won):,.0f}")
    contacts = db.query(Lead).filter(Lead.client_id == client.id).limit(20).all()
    if contacts:
        lines.append("\n## Linked contacts")
        for c in contacts:
            bits = [c.name]
            if c.job_title: bits.append(c.job_title)
            bits.append(c.status.value)
            lines.append("- " + " · ".join(bits))
    recent_acts = (
        db.query(Activity).filter(Activity.client_id == client.id)
        .order_by(Activity.created_at.desc()).limit(10).all()
    )
    if recent_acts:
        lines.append("\n## Recent activity")
        for a in recent_acts:
            when = a.created_at.strftime("%Y-%m-%d") if a.created_at else "?"
            preview = (a.body or "")[:200].replace("\n", " ")
            lines.append(f"- {when} · {a.type.value} · {a.subject} — {preview}")

    try:
        result = await draft_account_plan("\n".join(lines), db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    plan = _get_or_create_account_plan(db, client, user.id)
    fill_all = bool(overwrite)
    for field in ("business_goals", "whitespace", "key_stakeholders", "threats_risks", "next_90d_actions", "success_metrics"):
        val = (result.get(field) or "").strip()
        if not val:
            continue
        if fill_all or not getattr(plan, field):
            setattr(plan, field, val)
    db.commit()
    return flash(RedirectResponse(f"/clients/{client_id}#account-plan", 303),
                 "AI account plan drafted." + (" (overwrote existing sections)" if fill_all else " (filled empty sections only)"))

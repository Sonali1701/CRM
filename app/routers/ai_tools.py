"""AI tools — Gemini-powered helpers for staffing sales.

Endpoints:
  GET  /ai-tools                  — hub page with links
  GET  /ai-tools/meeting          — paste meeting notes UI
  POST /ai-tools/meeting          — Gemini → structured MOM + optional auto-create activities
  GET  /ai-tools/jd               — paste JD UI
  POST /ai-tools/jd               — Gemini → structured requirement
  POST /deals/{id}/suggest-step   — registered separately on deals router
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, Lead, Client, Activity, Deal
from app.models.activity import ActivityType
from app.services.ai_compose import (
    is_ai_configured, summarize_meeting, analyze_jd,
    handle_objection, generate_brief, draft_followup,
)
from app.templating import templates


router = APIRouter()


def _require_ai():
    if not is_ai_configured():
        raise HTTPException(
            400,
            "AI tools need at least one provider key. Set GEMINI_API_KEY "
            "(aistudio.google.com) and/or GROQ_API_KEY (console.groq.com) in env. "
            "Both have free tiers; no credit card needed.",
        )


@router.get("")
def ai_tools_hub(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "ai_tools/hub.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
    })


# ── Meeting Summarizer ───────────────────────────────────────────────────────

@router.get("/meeting")
def meeting_summary_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    # Lookups for the optional "attach to" pickers
    leads = (
        db.query(Lead)
        .filter(Lead.owner_id == user.id if not user.is_manager else True)
        .order_by(Lead.first_name)
        .limit(200)
        .all()
    )
    clients = db.query(Client).order_by(Client.name).limit(200).all()
    return templates.TemplateResponse(request, "ai_tools/meeting.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "leads": leads, "clients": clients,
        "result": None, "notes": "",
    })


@router.post("/meeting")
async def meeting_summary_post(
    request: Request,
    notes: str = Form(...),
    lead_id: str = Form(""),
    client_id: str = Form(""),
    create_activities: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    _require_ai()
    if not notes.strip():
        raise HTTPException(400, "Paste some meeting notes first")

    try:
        result = await summarize_meeting(notes, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    lid = int(lead_id) if lead_id.strip().isdigit() else None
    cid = int(client_id) if client_id.strip().isdigit() else None

    created_count = 0
    if create_activities:
        # 1) Log the meeting itself
        db.add(Activity(
            type=ActivityType.MEETING,
            subject=f"Meeting: {result.get('summary', '')[:200] or 'AI summarized meeting'}",
            body=_format_mom_text(result),
            lead_id=lid, client_id=cid,
            created_by_id=user.id, completed=True,
        ))
        # 2) One task per action item
        now = datetime.now(timezone.utc)
        for item in result.get("action_items", []) or []:
            action = (item or {}).get("action")
            if not action:
                continue
            due_days = (item or {}).get("due_in_days") or 7
            try:
                due_days = int(due_days)
            except (TypeError, ValueError):
                due_days = 7
            db.add(Activity(
                type=ActivityType.TASK,
                subject=action[:500],
                body=f"Owner: {(item or {}).get('owner', '—')}",
                lead_id=lid, client_id=cid,
                created_by_id=user.id,
                due_at=now + timedelta(days=due_days),
            ))
            created_count += 1
        db.commit()

    return templates.TemplateResponse(request, "ai_tools/meeting.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": True,
        "leads": db.query(Lead).order_by(Lead.first_name).limit(200).all(),
        "clients": db.query(Client).order_by(Client.name).limit(200).all(),
        "result": result, "notes": notes,
        "created_activities": created_count if create_activities else 0,
        "selected_lead_id": lid, "selected_client_id": cid,
    })


def _format_mom_text(r: dict) -> str:
    lines = []
    if r.get("summary"):
        lines.append("Summary: " + r["summary"])
    if r.get("attendees"):
        att_strs = []
        for a in r["attendees"]:
            if not a:
                continue
            if isinstance(a, str):
                att_strs.append(a)
            else:
                bits = [a.get("name") or "", a.get("role") or "", a.get("organisation") or ""]
                att_strs.append(" / ".join(b for b in bits if b))
        if att_strs:
            lines.append("Attendees: " + "; ".join(att_strs))
    if r.get("priorities"):
        lines.append("\nClient priorities:")
        lines.extend(f"- {p}" for p in r["priorities"])
    if r.get("key_points"):
        lines.append("\nKey points:")
        lines.extend(f"- {p}" for p in r["key_points"])
    if r.get("action_items"):
        lines.append("\nAction items:")
        for it in r["action_items"]:
            it = it or {}
            owner = it.get("owner", "")
            action = it.get("action", "")
            due = it.get("due_in_days", "")
            pri = it.get("priority", "")
            tag = f" [{pri}]" if pri else ""
            lines.append(f"- [{owner}]{tag} {action} (due in {due}d)")
    if r.get("requirements"):
        lines.append("\nStaffing requirements raised:")
        for req in r["requirements"]:
            req = req or {}
            skills = req.get("skills") or []
            skills_str = ", ".join(skills) if skills else ""
            lines.append(
                f"- {req.get('role', '')} ({req.get('count', '')}) · "
                f"{req.get('contract_type', '')} · {req.get('experience_years', 0)}y+ · "
                f"{req.get('location', '')} · rate {req.get('rate', '')} · "
                f"duration {req.get('duration', '')} · starts {req.get('start_date', '')} · "
                f"urgency {req.get('urgency', '')}"
            )
            if skills_str:
                lines.append(f"    Skills: {skills_str}")
    if r.get("next_meeting"):
        nm = r["next_meeting"] or {}
        if nm.get("scheduled") or nm.get("agenda"):
            lines.append(f"\nNext meeting: {nm.get('scheduled', '')} — {nm.get('agenda', '')}")
    return "\n".join(lines)


# ── JD / Requirement Analyzer ────────────────────────────────────────────────

@router.get("/jd")
def jd_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "ai_tools/jd.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "result": None, "jd_text": "",
    })


@router.post("/jd")
async def jd_post(
    request: Request,
    jd_text: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_ai()
    if not jd_text.strip():
        raise HTTPException(400, "Paste a JD first")
    try:
        result = await analyze_jd(jd_text, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/jd.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": True,
        "result": result, "jd_text": jd_text,
    })


# ── Objection Handler ───────────────────────────────────────────────────────

@router.get("/objection")
def objection_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "ai_tools/objection.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "result": None, "objection": "", "context": "",
    })


@router.post("/objection")
async def objection_post(
    request: Request,
    objection: str = Form(...),
    context: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_ai()
    if not objection.strip():
        raise HTTPException(400, "Paste the objection first")
    try:
        result = await handle_objection(objection, context, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/objection.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": True,
        "result": result, "objection": objection, "context": context,
    })


# ── Pre-meeting research brief ──────────────────────────────────────────────

def _build_company_brief_context(db: Session, client: Client) -> str:
    """Assemble all-we-know-about-this-company into a plain-text block for the LLM."""
    lines = [f"# Company: {client.name}"]
    bits = []
    if client.industry: bits.append(f"industry: {client.industry}")
    if client.website: bits.append(f"website: {client.website}")
    if client.type: bits.append(f"type: {client.type.value}")
    hq = ", ".join(filter(None, [client.hq_city, client.hq_state, client.hq_country]))
    if hq: bits.append(f"HQ: {hq}")
    if bits: lines.append(" · ".join(bits))
    if client.description: lines.append(f"\nDescription:\n{client.description}")
    if client.notes: lines.append(f"\nInternal notes:\n{client.notes}")

    # Linked contacts
    contacts = (
        db.query(Lead).filter(Lead.client_id == client.id)
        .order_by(Lead.first_name).limit(20).all()
    )
    if contacts:
        lines.append("\n## Linked contacts")
        for c in contacts:
            line = f"- {c.name}"
            if c.job_title: line += f" ({c.job_title})"
            if c.email: line += f" · {c.email}"
            line += f" · status: {c.status.value}"
            lines.append(line)

    # Open deals
    from app.models.deal import OPEN_STAGES
    open_deals = (
        db.query(Deal).filter(Deal.client_id == client.id, Deal.stage.in_(OPEN_STAGES))
        .order_by(Deal.updated_at.desc()).limit(10).all()
    )
    if open_deals:
        lines.append("\n## Open deals")
        for d in open_deals:
            lines.append(f"- {d.title} · stage: {d.stage_label} · value: {d.value} {d.currency}")

    # Recent activities
    recent_acts = (
        db.query(Activity)
        .filter(Activity.client_id == client.id)
        .order_by(Activity.created_at.desc()).limit(15).all()
    )
    if recent_acts:
        lines.append("\n## Recent activity (most recent first)")
        for a in recent_acts:
            date = a.created_at.strftime("%Y-%m-%d") if a.created_at else "?"
            preview = (a.body or "")[:200].replace("\n", " ")
            lines.append(f"- {date} · {a.type.value} · {a.subject} — {preview}")

    return "\n".join(lines)


def _build_lead_brief_context(db: Session, lead: Lead) -> str:
    lines = [f"# Contact: {lead.name}"]
    bits = []
    if lead.job_title: bits.append(f"title: {lead.job_title}")
    if lead.company: bits.append(f"company: {lead.company}")
    if lead.email: bits.append(f"email: {lead.email}")
    if lead.location: bits.append(f"location: {lead.location}")
    bits.append(f"status: {lead.status.value}")
    if bits: lines.append(" · ".join(bits))
    if lead.notes: lines.append(f"\nNotes:\n{lead.notes}")

    # Linked company
    if lead.client_id:
        client = db.get(Client, lead.client_id)
        if client:
            lines.append(f"\n## Linked company\n{client.name}"
                         + (f" — {client.industry}" if client.industry else "")
                         + (f"\n{client.description}" if client.description else ""))

    # Recent activities with this contact
    recent_acts = (
        db.query(Activity).filter(Activity.lead_id == lead.id)
        .order_by(Activity.created_at.desc()).limit(15).all()
    )
    if recent_acts:
        lines.append("\n## Recent activity")
        for a in recent_acts:
            date = a.created_at.strftime("%Y-%m-%d") if a.created_at else "?"
            preview = (a.body or "")[:200].replace("\n", " ")
            lines.append(f"- {date} · {a.type.value} · {a.subject} — {preview}")

    return "\n".join(lines)


@router.get("/brief/company/{client_id}")
async def brief_company(client_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _require_ai()
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(404)
    if not user.is_manager and client.owner_id and client.owner_id != user.id:
        raise HTTPException(403)
    context_text = _build_company_brief_context(db, client)
    try:
        result = await generate_brief(context_text, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/brief.html", {
        "user": user, "flash": get_flash(request),
        "target_kind": "Company", "target": client,
        "back_url": f"/clients/{client.id}",
        "result": result,
    })


@router.get("/brief/contact/{lead_id}")
async def brief_contact(lead_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _require_ai()
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    if not user.is_manager and lead.owner_id and lead.owner_id != user.id:
        raise HTTPException(403)
    context_text = _build_lead_brief_context(db, lead)
    try:
        result = await generate_brief(context_text, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/brief.html", {
        "user": user, "flash": get_flash(request),
        "target_kind": "Contact", "target": lead,
        "back_url": f"/leads/{lead.id}",
        "result": result,
    })


# ── Follow-up email drafter (called via fetch from activity rows) ───────────

@router.post("/followup/{activity_id}")
async def followup_draft(
    activity_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_ai()
    activity = db.get(Activity, activity_id)
    if not activity:
        raise HTTPException(404)
    if not user.is_manager and activity.created_by_id != user.id:
        raise HTTPException(403)

    # Build a concise summary of what happened
    summary_parts = [
        f"Type: {activity.type.value}",
        f"Subject: {activity.subject}",
    ]
    if activity.body:
        summary_parts.append(f"Notes:\n{activity.body[:1500]}")
    if activity.created_at:
        summary_parts.append(f"Date: {activity.created_at.strftime('%Y-%m-%d')}")
    summary = "\n".join(summary_parts)

    contact = {}
    if activity.lead:
        contact = {
            "name": activity.lead.name,
            "first_name": activity.lead.first_name,
            "company": activity.lead.company or "",
            "title": activity.lead.job_title or "",
            "email": activity.lead.email or "",
        }

    try:
        result = await draft_followup(summary, contact, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {
        "subject": result["subject"],
        "body": result["body"],
        "to": (activity.lead.email if activity.lead else "") or "",
        "lead_id": activity.lead_id,
    }

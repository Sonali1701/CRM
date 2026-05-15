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
    draft_capability, generate_proposal, explain_deal_risk,
    daily_focus, map_stakeholders, draft_social_messages,
    draft_sow, analyze_winloss, advise_strategy, coach_rep,
    classify_outreach_notes,
)
from app.models.deal import DealStage, OPEN_STAGES, STAGE_LABELS
from app.services.deal_risk import compute_risk
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


# ── Capability statement drafter ────────────────────────────────────────────

@router.get("/capability")
def capability_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "ai_tools/capability.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "result": None, "client_domain": "", "use_case": "",
    })


@router.post("/capability")
async def capability_post(
    request: Request,
    client_domain: str = Form(...),
    use_case: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_ai()
    if not client_domain.strip():
        raise HTTPException(400, "Enter the prospect's industry/domain")
    try:
        result = await draft_capability(client_domain, use_case, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/capability.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": True,
        "result": result, "client_domain": client_domain, "use_case": use_case,
    })


# ── Proposal generator (called from deal page) ──────────────────────────────

def _build_deal_proposal_context(db: Session, deal: Deal) -> str:
    """Assemble deal + client + recent activities + extracted requirements
    into a plain-text block for the proposal LLM call."""
    lines = [f"# Deal: {deal.title}"]
    bits = [
        f"stage: {deal.stage_label}",
        f"value: {deal.value} {deal.currency}",
        f"probability: {deal.probability}%",
    ]
    if deal.expected_close_date:
        bits.append(f"target close: {deal.expected_close_date}")
    lines.append(" · ".join(bits))
    if deal.notes:
        lines.append(f"\nDeal notes:\n{deal.notes}")

    # Client
    if deal.client_id:
        client = db.get(Client, deal.client_id)
        if client:
            lines.append(f"\n## Client: {client.name}")
            cbits = []
            if client.industry: cbits.append(f"industry: {client.industry}")
            hq = ", ".join(filter(None, [client.hq_city, client.hq_state, client.hq_country]))
            if hq: cbits.append(f"HQ: {hq}")
            if cbits: lines.append(" · ".join(cbits))
            if client.description: lines.append(f"\n{client.description}")

    # Recent activities — especially any auto-MOMs from meeting summarizer
    recent_acts = (
        db.query(Activity)
        .filter(
            (Activity.deal_id == deal.id) | (Activity.client_id == deal.client_id),
        )
        .order_by(Activity.created_at.desc())
        .limit(10)
        .all()
    )
    if recent_acts:
        lines.append("\n## Recent activity / meeting MOMs")
        for a in recent_acts:
            date = a.created_at.strftime("%Y-%m-%d") if a.created_at else "?"
            body = (a.body or "")[:800]
            lines.append(f"\n--- {date} · {a.type.value} · {a.subject} ---\n{body}")

    return "\n".join(lines)


@router.post("/risk/{deal_id}")
async def risk_explain(
    deal_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_ai()
    deal = db.get(Deal, deal_id)
    if not deal:
        raise HTTPException(404)
    if not user.is_manager and deal.owner_id and deal.owner_id != user.id:
        raise HTTPException(403)
    from app.services.deal_risk import compute_risk, risk_context_string
    risk = compute_risk(deal)
    context_text = risk_context_string(deal, risk)
    try:
        result = await explain_deal_risk(context_text, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {
        "level": risk.level,
        "rule_reasons": risk.reasons,
        "ai_reason": result.get("reason"),
        "ai_action": result.get("action"),
        "ai_urgency": result.get("urgency"),
        "days_since_activity": risk.days_since_activity,
        "days_in_stage": risk.days_in_stage,
    }


@router.post("/proposal/{deal_id}")
async def proposal_post(
    deal_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _require_ai()
    deal = db.get(Deal, deal_id)
    if not deal:
        raise HTTPException(404)
    if not user.is_manager and deal.owner_id and deal.owner_id != user.id:
        raise HTTPException(403)
    context_text = _build_deal_proposal_context(db, deal)
    try:
        result = await generate_proposal(context_text, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/proposal.html", {
        "user": user, "flash": get_flash(request),
        "deal": deal, "result": result,
    })


# ── Smart Daily Briefing (on-demand) ────────────────────────────────────────

def _build_briefing_context(db: Session, user: User) -> dict:
    """Collect today's signals for `user`: overdue tasks, today's tasks, risky
    deals, hot deals. Returns both the structured data (for the template) and
    a flat text block (for the AI focus call)."""
    now = datetime.now(timezone.utc)
    end_of_today = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days_ago = now - timedelta(days=7)

    acts_q = db.query(Activity)
    deals_q = db.query(Deal).filter(Deal.stage.in_(OPEN_STAGES))
    if not user.is_manager:
        acts_q = acts_q.filter(Activity.created_by_id == user.id)
        deals_q = deals_q.filter(Deal.owner_id == user.id)

    overdue = (
        acts_q.filter(Activity.completed == False, Activity.due_at < now, Activity.due_at.isnot(None))
        .order_by(Activity.due_at)
        .limit(20)
        .all()
    )
    today_acts = (
        acts_q.filter(Activity.completed == False, Activity.due_at >= now, Activity.due_at < end_of_today)
        .order_by(Activity.due_at)
        .limit(20)
        .all()
    )

    open_deals = deals_q.all()
    risky_deals: list[tuple] = []
    for d in open_deals:
        r = compute_risk(d)
        if r.level in ("high", "medium"):
            risky_deals.append((d, r))
    risky_deals.sort(key=lambda t: (0 if t[1].level == "high" else 1, -(t[1].days_since_activity or 0)))
    risky_deals = risky_deals[:10]

    hot_deals: list = []
    if open_deals:
        deal_ids = [d.id for d in open_deals]
        rows = (
            db.query(Activity.deal_id)
            .filter(Activity.deal_id.in_(deal_ids), Activity.created_at >= seven_days_ago)
            .all()
        )
        counts: dict[int, int] = {}
        for row in rows:
            if row[0]:
                counts[row[0]] = counts.get(row[0], 0) + 1
        id_to_deal = {d.id: d for d in open_deals}
        hot_deals = [id_to_deal[did] for did, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:5]]

    return {
        "now": now,
        "overdue": overdue,
        "today_acts": today_acts,
        "risky_deals": risky_deals,
        "hot_deals": hot_deals,
        "open_deal_count": len(open_deals),
    }


@router.get("/briefing")
async def briefing_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    data = _build_briefing_context(db, user)
    focus = None
    if is_ai_configured() and (data["overdue"] or data["today_acts"] or data["risky_deals"] or data["hot_deals"]):
        ctx_parts = [f"Rep: {user.full_name}"]
        if data["overdue"]:
            ctx_parts.append(f"Overdue tasks: {len(data['overdue'])} — top: " +
                             "; ".join(a.subject for a in data["overdue"][:3]))
        if data["today_acts"]:
            ctx_parts.append(f"Tasks due today: {len(data['today_acts'])} — top: " +
                             "; ".join(a.subject for a in data["today_acts"][:3]))
        if data["risky_deals"]:
            ctx_parts.append("At-risk deals: " + "; ".join(
                f"{d.title} ({r.level}, {r.days_since_activity}d quiet)"
                for d, r in data["risky_deals"][:5]
            ))
        if data["hot_deals"]:
            ctx_parts.append("Hot deals (most active last 7d): " + "; ".join(
                f"{d.title} ({d.stage_label})" for d in data["hot_deals"][:3]
            ))
        try:
            focus = await daily_focus("\n".join(ctx_parts), db=db)
        except Exception as e:
            print(f"[briefing] daily_focus failed: {e}")

    return templates.TemplateResponse(request, "ai_tools/briefing.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "focus": focus,
        **data,
    })


# ── AI Stakeholder Mapper ───────────────────────────────────────────────────

def _build_stakeholder_context(db: Session, client: Client) -> str:
    lines = [f"# Account: {client.name}"]
    if client.industry:
        lines.append(f"Industry: {client.industry}")
    contacts = db.query(Lead).filter(Lead.client_id == client.id).all()
    if not contacts:
        lines.append("No linked contacts yet.")
    else:
        lines.append("\n## Contacts and their signals")
        for c in contacts:
            recent = (
                db.query(Activity).filter(Activity.lead_id == c.id)
                .order_by(Activity.created_at.desc()).limit(5).all()
            )
            last_touch = recent[0].created_at.strftime("%Y-%m-%d") if recent else "never"
            line = f"- {c.name}"
            if c.job_title:
                line += f" — {c.job_title}"
            line += f" · status: {c.status.value}"
            line += f" · last touch: {last_touch}"
            line += f" · recent activity count: {len(recent)}"
            lines.append(line)
    return "\n".join(lines)


@router.get("/stakeholders/{client_id}")
async def stakeholders_page(client_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _require_ai()
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(404)
    if not user.is_manager and client.owner_id and client.owner_id != user.id:
        raise HTTPException(403)
    try:
        result = await map_stakeholders(_build_stakeholder_context(db, client), db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/stakeholders.html", {
        "user": user, "flash": get_flash(request),
        "client": client, "result": result,
    })


# ── LinkedIn / WhatsApp message drafter ─────────────────────────────────────

@router.get("/social")
def social_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    leads = db.query(Lead).order_by(Lead.first_name).limit(200).all() if user.is_manager else \
            db.query(Lead).filter(Lead.owner_id == user.id).order_by(Lead.first_name).limit(200).all()
    return templates.TemplateResponse(request, "ai_tools/social.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "leads": leads, "result": None, "intent": "", "selected_lead_id": "",
    })


@router.post("/social")
async def social_post(
    request: Request,
    intent: str = Form(...),
    lead_id: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    _require_ai()
    if not intent.strip():
        raise HTTPException(400, "Describe what you want to say")
    contact_ctx = ""
    lid = int(lead_id) if lead_id.strip().isdigit() else None
    if lid:
        lead = db.get(Lead, lid)
        if lead:
            bits = [f"Name: {lead.name}"]
            if lead.job_title: bits.append(f"Title: {lead.job_title}")
            if lead.company: bits.append(f"Company: {lead.company}")
            contact_ctx = " · ".join(bits)
    try:
        result = await draft_social_messages(intent, contact_ctx, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    leads = db.query(Lead).order_by(Lead.first_name).limit(200).all() if user.is_manager else \
            db.query(Lead).filter(Lead.owner_id == user.id).order_by(Lead.first_name).limit(200).all()
    return templates.TemplateResponse(request, "ai_tools/social.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": True, "leads": leads,
        "result": result, "intent": intent, "selected_lead_id": lead_id,
    })


# ── Rate Card Recommender (internal data, no AI required) ───────────────────

def _rate_card_for(db: Session, role_query: str) -> dict:
    """Find similar past deals (matching role keywords in title or notes) and
    return median / avg deal value as a proxy for rate. Pure SQL — no AI call."""
    needles = [t.strip().lower() for t in role_query.split() if t.strip() and len(t.strip()) > 2]
    if not needles:
        return {"matches": [], "median": None, "avg": None, "min": None, "max": None}
    # Match either title or notes
    from sqlalchemy import or_
    conditions = []
    for n in needles:
        like = f"%{n}%"
        conditions.append(Deal.title.ilike(like))
        conditions.append(Deal.notes.ilike(like))
    q = db.query(Deal).filter(or_(*conditions), Deal.value > 0)
    deals = q.order_by(Deal.updated_at.desc()).limit(50).all()
    values = sorted(float(d.value) for d in deals if d.value)
    summary = {"matches": deals[:10]}
    if values:
        summary["min"] = values[0]
        summary["max"] = values[-1]
        summary["avg"] = sum(values) / len(values)
        mid = len(values) // 2
        summary["median"] = values[mid] if len(values) % 2 == 1 else (values[mid - 1] + values[mid]) / 2
    else:
        summary["min"] = summary["max"] = summary["avg"] = summary["median"] = None
    return summary


@router.get("/rates")
def rates_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "ai_tools/rates.html", {
        "user": user, "flash": get_flash(request),
        "role_query": "", "summary": None,
    })


@router.post("/rates")
def rates_post(
    request: Request,
    role_query: str = Form(...),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    summary = _rate_card_for(db, role_query)
    return templates.TemplateResponse(request, "ai_tools/rates.html", {
        "user": user, "flash": get_flash(request),
        "role_query": role_query, "summary": summary,
    })


# ── SOW Drafter (per deal) ──────────────────────────────────────────────────

@router.post("/sow/{deal_id}")
async def sow_post(
    deal_id: int,
    request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    _require_ai()
    deal = db.get(Deal, deal_id)
    if not deal:
        raise HTTPException(404)
    if not user.is_manager and deal.owner_id and deal.owner_id != user.id:
        raise HTTPException(403)
    context_text = _build_deal_proposal_context(db, deal)
    try:
        result = await draft_sow(context_text, db=db)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return templates.TemplateResponse(request, "ai_tools/sow.html", {
        "user": user, "flash": get_flash(request),
        "deal": deal, "result": result,
    })


# ── Win/Loss Analyzer ───────────────────────────────────────────────────────

def _build_winloss_context(db: Session, user: User) -> tuple[str, int, int]:
    """Recent closed deals (last 180d), text-formatted for the AI prompt.
    Returns (context_text, won_count, lost_count)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    q = db.query(Deal).filter(
        Deal.stage.in_([DealStage.CLOSED_WON, DealStage.CLOSED_LOST]),
        Deal.updated_at >= cutoff,
    )
    if not user.is_manager:
        q = q.filter(Deal.owner_id == user.id)
    deals = q.order_by(Deal.updated_at.desc()).limit(50).all()
    won = [d for d in deals if d.stage == DealStage.CLOSED_WON]
    lost = [d for d in deals if d.stage == DealStage.CLOSED_LOST]
    lines = [f"Closed deals in the last 180 days: {len(won)} won, {len(lost)} lost."]
    for d in deals:
        bits = [d.title, d.stage.value, f"value={d.value}"]
        if d.client:
            bits.append(f"client={d.client.name}")
            if d.client.industry:
                bits.append(f"industry={d.client.industry}")
        if d.notes:
            bits.append(f"notes={d.notes[:200]}")
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines), len(won), len(lost)


@router.get("/winloss")
async def winloss_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx, won, lost = _build_winloss_context(db, user)
    result = None
    if is_ai_configured() and (won + lost) >= 2:
        try:
            result = await analyze_winloss(ctx, db=db)
        except Exception as e:
            print(f"[winloss] {e}")
    return templates.TemplateResponse(request, "ai_tools/winloss.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "won": won, "lost": lost, "result": result,
    })


# ── Enterprise Strategy Advisor ─────────────────────────────────────────────

def _build_strategy_context(db: Session, user: User) -> str:
    deals_q = db.query(Deal)
    if not user.is_manager:
        deals_q = deals_q.filter(Deal.owner_id == user.id)
    deals = deals_q.all()
    industry_stats: dict[str, dict] = {}
    for d in deals:
        ind = (d.client.industry if d.client else None) or "Unknown"
        s = industry_stats.setdefault(ind, {"open": 0, "won": 0, "lost": 0, "value_open": 0.0, "value_won": 0.0})
        if d.stage == DealStage.CLOSED_WON:
            s["won"] += 1; s["value_won"] += float(d.value or 0)
        elif d.stage == DealStage.CLOSED_LOST:
            s["lost"] += 1
        elif d.stage in OPEN_STAGES:
            s["open"] += 1; s["value_open"] += float(d.value or 0)
    # Top accounts by open pipeline value
    open_deals = [d for d in deals if d.stage in OPEN_STAGES]
    accounts: dict[int, dict] = {}
    for d in open_deals:
        if not d.client_id:
            continue
        a = accounts.setdefault(d.client_id, {"name": d.client.name if d.client else f"#{d.client_id}", "value": 0.0, "count": 0, "industry": (d.client.industry if d.client else "")})
        a["value"] += float(d.value or 0); a["count"] += 1
    top_accounts = sorted(accounts.values(), key=lambda x: -x["value"])[:8]

    lines = ["# Pipeline by industry"]
    for ind, s in sorted(industry_stats.items(), key=lambda kv: -kv[1]["value_open"]):
        lines.append(
            f"- {ind}: open={s['open']} (${int(s['value_open']):,}) · won={s['won']} (${int(s['value_won']):,}) · lost={s['lost']}"
        )
    lines.append("\n# Top open accounts")
    for a in top_accounts:
        ind = f" [{a['industry']}]" if a["industry"] else ""
        lines.append(f"- {a['name']}{ind}: {a['count']} open deals, ${int(a['value']):,}")
    return "\n".join(lines)


@router.get("/strategy")
async def strategy_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    result = None
    ctx = _build_strategy_context(db, user)
    if is_ai_configured():
        try:
            result = await advise_strategy(ctx, db=db)
        except Exception as e:
            print(f"[strategy] {e}")
    return templates.TemplateResponse(request, "ai_tools/strategy.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "raw_context": ctx, "result": result,
    })


# ── Sales Coach ─────────────────────────────────────────────────────────────

def _build_coach_context(db: Session, user: User) -> str:
    """Summarize the rep's activity last 30 days: counts by type, response gap,
    deals worked, win rate."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    target_user_id = user.id

    acts = db.query(Activity).filter(
        Activity.created_by_id == target_user_id,
        Activity.created_at >= cutoff,
    ).all()
    by_type: dict[str, int] = {}
    for a in acts:
        by_type[a.type.value] = by_type.get(a.type.value, 0) + 1

    open_deals = db.query(Deal).filter(
        Deal.owner_id == target_user_id, Deal.stage.in_(OPEN_STAGES),
    ).all()
    won = db.query(Deal).filter(Deal.owner_id == target_user_id, Deal.stage == DealStage.CLOSED_WON).count()
    lost = db.query(Deal).filter(Deal.owner_id == target_user_id, Deal.stage == DealStage.CLOSED_LOST).count()

    # Stalled = no activity in 14+ days
    stalled = []
    for d in open_deals:
        last = d.last_activity_at
        if last is None or (now - (last if last.tzinfo else last.replace(tzinfo=timezone.utc))).days >= 14:
            stalled.append(d)

    lines = [f"Rep: {user.full_name}",
             f"Activities last 30d: total={len(acts)}, by type: " + ", ".join(f"{k}={v}" for k, v in by_type.items()),
             f"Open deals: {len(open_deals)}; stalled (no activity 14d+): {len(stalled)}",
             f"Closed last all-time: won={won}, lost={lost}"]
    if stalled[:5]:
        lines.append("Stalled deals: " + "; ".join(d.title for d in stalled[:5]))
    return "\n".join(lines)


@router.get("/coach")
async def coach_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    result = None
    ctx = _build_coach_context(db, user)
    if is_ai_configured():
        try:
            result = await coach_rep(ctx, db=db)
        except Exception as e:
            print(f"[coach] {e}")
    return templates.TemplateResponse(request, "ai_tools/coach.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": is_ai_configured(),
        "raw_context": ctx, "result": result,
    })


# ── Outreach Insights (classifies free-text outreach notes across contacts) ──

OUTREACH_CATEGORY_LABELS = {
    "info_sent": ("Info requested", "Asked us to send company info — weak positive"),
    "not_interested_now": ("Not interested now", "No empanelment / no requirements"),
    "wrong_poc": ("Wrong POC", "Suggested a different person"),
    "out_of_org": ("Out of org", "Left company / laid off"),
    "reconnect_later": ("Reconnect later", "Asked to be pinged at a future date"),
    "in_house_only": ("In-house only", "They hire internally; no vendors"),
    "positive": ("Positive", "Active engagement / forward momentum"),
    "no_outcome": ("No clear outcome", "Note exists but signal unclear"),
}

OUTREACH_CATEGORY_TONES = {
    "positive": "bg-emerald-100 text-emerald-700",
    "info_sent": "bg-blue-100 text-blue-700",
    "reconnect_later": "bg-indigo-100 text-indigo-700",
    "wrong_poc": "bg-amber-100 text-amber-700",
    "not_interested_now": "bg-slate-100 text-slate-600",
    "in_house_only": "bg-slate-100 text-slate-600",
    "out_of_org": "bg-red-100 text-red-700",
    "no_outcome": "bg-slate-100 text-slate-400",
}


def _parse_iso_date(s: str):
    """Accept 'YYYY-MM-DD' or 'YYYY-MM'. Returns date or None."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _notes_hash(text: str | None) -> str:
    """Short stable hash of a note — used to detect whether re-classification
    is needed since last AI call."""
    import hashlib
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16] if text else ""


@router.get("/outreach")
async def outreach_insights(
    request: Request,
    force: str = "",
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Classify outreach notes across leads. Only leads whose notes have
    CHANGED since last classification are re-sent to AI (or all of them
    if `force=1`). Results are persisted on Lead.* so other tabs read
    from cache without burning AI quota."""
    _require_ai()

    q = db.query(Lead).filter(Lead.notes.isnot(None), Lead.notes != "")
    if not user.is_manager:
        q = q.filter(Lead.owner_id == user.id)
    leads = q.order_by(Lead.updated_at.desc()).limit(500).all()

    if not leads:
        return templates.TemplateResponse(request, "ai_tools/outreach.html", {
            "user": user, "flash": get_flash(request),
            "ai_enabled": True,
            "rows": [], "totals": {}, "buckets": {},
            "category_labels": OUTREACH_CATEGORY_LABELS,
            "category_tones": OUTREACH_CATEGORY_TONES,
            "empty_reason": "No contacts have notes yet. Add outreach notes on contacts to see insights here.",
            "stale_count": 0, "fresh_count": 0,
        })

    force_all = bool(force)
    to_classify: list[Lead] = []
    fresh_count = 0
    for lead in leads:
        current_hash = _notes_hash(lead.notes)
        if force_all or not lead.outreach_category or lead.outreach_notes_hash != current_hash:
            to_classify.append(lead)
        else:
            fresh_count += 1

    # Batch-classify only the stale ones
    if to_classify:
        chunk_size = 25
        by_id: dict[int, dict] = {}
        for i in range(0, len(to_classify), chunk_size):
            chunk = to_classify[i:i + chunk_size]
            try:
                result = await classify_outreach_notes(
                    [{"note_id": l.id, "text": l.notes} for l in chunk], db=db,
                )
            except RuntimeError as e:
                raise HTTPException(502, str(e))
            for r in (result.get("results") or []):
                by_id[int(r.get("note_id"))] = r

        now = datetime.now(timezone.utc)
        for lead in to_classify:
            cls = by_id.get(lead.id) or {"category": "no_outcome"}
            lead.outreach_category = cls.get("category", "no_outcome")
            lead.outreach_summary = (cls.get("key_reason") or "").strip() or None
            lead.outreach_suggested_poc = (cls.get("suggested_poc_name") or "").strip() or None
            lead.outreach_reconnect_date = _parse_iso_date(cls.get("reconnect_date", ""))
            lead.outreach_notes_hash = _notes_hash(lead.notes)
            lead.outreach_classified_at = now
        db.commit()

    # Build view rows from the now-cached fields
    rows = []
    for lead in leads:
        rows.append({
            "lead": lead,
            "note_text": lead.notes or "",
            "category": lead.outreach_category or "no_outcome",
            "reconnect_date": lead.outreach_reconnect_date,
            "suggested_poc_name": lead.outreach_suggested_poc or "",
            "key_reason": lead.outreach_summary or "",
        })

    buckets: dict[str, list] = {k: [] for k in OUTREACH_CATEGORY_LABELS.keys()}
    for r in rows:
        buckets.setdefault(r["category"], []).append(r)

    totals = {"analyzed": len(rows)}
    for k, v in buckets.items():
        totals[k] = len(v)

    buckets["reconnect_later"].sort(key=lambda r: (r["reconnect_date"] is None, r["reconnect_date"]))

    return templates.TemplateResponse(request, "ai_tools/outreach.html", {
        "user": user, "flash": get_flash(request),
        "ai_enabled": True,
        "rows": rows, "totals": totals, "buckets": buckets,
        "category_labels": OUTREACH_CATEGORY_LABELS,
        "category_tones": OUTREACH_CATEGORY_TONES,
        "empty_reason": None,
        "stale_count": len(to_classify),
        "fresh_count": fresh_count,
    })


@router.post("/outreach/mark-inactive/{lead_id}")
def outreach_mark_inactive(
    lead_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """One-click: mark a contact as Disqualified (used for out-of-org cases)."""
    from app.models.lead import LeadStatus
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    if not user.is_manager and lead.owner_id and lead.owner_id != user.id:
        raise HTTPException(403)
    lead.status = LeadStatus.DISQUALIFIED
    db.commit()
    return flash(RedirectResponse("/ai-tools/outreach", 303), f"{lead.name} marked disqualified.", "error")


@router.post("/outreach/schedule-reconnect/{lead_id}")
def outreach_schedule_reconnect(
    lead_id: int,
    reconnect_date: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """One-click: create a TASK reminder on the parsed reconnect date."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    if not user.is_manager and lead.owner_id and lead.owner_id != user.id:
        raise HTTPException(403)
    parsed = _parse_iso_date(reconnect_date)
    if not parsed:
        raise HTTPException(400, "Need a valid reconnect date")
    due = datetime.combine(parsed, datetime.min.time()).replace(tzinfo=timezone.utc)
    task = Activity(
        type=ActivityType.TASK,
        subject=f"Reconnect with {lead.name}",
        body=f"Auto-scheduled from outreach insights — {lead.company or 'no company'} asked to be pinged on {parsed.strftime('%b %Y')}.",
        client_id=lead.client_id,
        lead_id=lead.id,
        created_by_id=user.id,
        due_at=due,
        completed=False,
    )
    db.add(task)
    db.commit()
    return flash(RedirectResponse("/ai-tools/outreach", 303), f"Reconnect task scheduled for {parsed.strftime('%b %d, %Y')}.")

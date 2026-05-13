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
from app.models import User, Lead, Client, Activity
from app.models.activity import ActivityType
from app.services.ai_compose import (
    is_ai_configured, summarize_meeting, analyze_jd,
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

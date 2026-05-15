from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, Activity, Deal, Lead, EmailMessage
from app.models.activity import ActivityType
from app.templating import templates

router = APIRouter()


def _inbound_activity_ids(db: Session, activities: list[Activity]) -> set[int]:
    """Return the subset of activity IDs that correspond to inbound emails.
    One batched query so the activities list doesn't N+1."""
    email_ids = [a.id for a in activities if a.type == ActivityType.EMAIL]
    if not email_ids:
        return set()
    rows = (
        db.query(EmailMessage.activity_id)
        .filter(EmailMessage.activity_id.in_(email_ids), EmailMessage.is_inbound == True)
        .all()
    )
    return {r[0] for r in rows}


def _suggested_followups(db: Session, user: User, limit: int = 10) -> list[Activity]:
    """Recent emails (last 14d) to a lead that don't already have a pending TASK
    activity for the same lead. Surfaced so an empty Upcoming section never
    leaves the user without a next step."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    lead_ids_with_pending = (
        select(Activity.lead_id)
        .where(
            Activity.type == ActivityType.TASK,
            Activity.completed == False,
            Activity.lead_id.isnot(None),
        )
    )
    q = (
        db.query(Activity)
        .filter(
            Activity.type == ActivityType.EMAIL,
            Activity.lead_id.isnot(None),
            Activity.created_at >= cutoff,
            Activity.lead_id.notin_(lead_ids_with_pending),
        )
    )
    if not user.is_manager:
        q = q.filter(Activity.created_by_id == user.id)
    return q.order_by(Activity.created_at.desc()).limit(limit).all()


@router.get("")
def activities_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = db.query(Activity)
    if not user.is_manager:
        q = q.filter(Activity.created_by_id == user.id)
    overdue = q.filter(Activity.completed == False, Activity.due_at < datetime.now(timezone.utc), Activity.due_at.isnot(None)).order_by(Activity.due_at).all()
    upcoming = q.filter(Activity.completed == False, Activity.due_at >= datetime.now(timezone.utc)).order_by(Activity.due_at).limit(20).all()
    recent = q.filter(Activity.completed == True).order_by(Activity.completed_at.desc().nullslast(), Activity.created_at.desc()).limit(30).all()
    suggested = _suggested_followups(db, user) if not upcoming else []
    inbound_ids = _inbound_activity_ids(db, recent + overdue + upcoming)
    return templates.TemplateResponse(request, "activities/list.html", {
        "user": user, "flash": get_flash(request),
        "overdue": overdue, "upcoming": upcoming, "recent": recent,
        "suggested": suggested,
        "inbound_ids": inbound_ids,
        "activity_types": list(ActivityType),
    })


@router.post("/{activity_id}/schedule-followup")
def schedule_followup(
    activity_id: int,
    days: str = Form("3"),
    redirect_to: str = Form("/activities"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """One-click: create a TASK reminder for the lead linked to this activity."""
    src = _get_activity(activity_id, user, db)
    if not src.lead_id:
        raise HTTPException(400, "Activity has no linked contact")
    try:
        n = max(1, min(60, int(days)))
    except (ValueError, TypeError):
        n = 3
    lead = db.get(Lead, src.lead_id)
    if not lead:
        raise HTTPException(404, "Contact not found")
    subj_short = (src.subject or "email")[:200]
    task = Activity(
        type=ActivityType.TASK,
        subject=f"Follow up with {lead.name}: {subj_short}",
        body=f"Reminder scheduled {n} days after \"{subj_short}\". Check for reply and decide next step.",
        client_id=lead.client_id,
        lead_id=lead.id,
        deal_id=src.deal_id,
        created_by_id=user.id,
        due_at=datetime.now(timezone.utc) + timedelta(days=n),
        completed=False,
    )
    db.add(task)
    db.commit()
    return flash(RedirectResponse(redirect_to, 303), f"Follow-up scheduled for {task.due_at.strftime('%b %d')}.")


@router.post("/new")
def activity_create(
    request: Request,
    type_: str = Form(...), subject: str = Form(...), body: str = Form(""),
    deal_id: str = Form(""), client_id: str = Form(""), due_at: str = Form(""),
    redirect_to: str = Form("/activities"),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    due = datetime.strptime(due_at, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) if due_at else None
    did = int(deal_id) if deal_id.strip().isdigit() else None
    cid = int(client_id) if client_id.strip().isdigit() else None
    activity = Activity(
        type=ActivityType(type_), subject=subject, body=body or None,
        deal_id=did, client_id=cid,
        due_at=due, created_by_id=user.id,
    )
    db.add(activity)
    if did:
        deal = db.get(Deal, did)
        if deal:
            deal.last_activity_at = datetime.now(timezone.utc)
    db.commit()
    return flash(RedirectResponse(redirect_to, 303), "Activity logged.")


@router.post("/{activity_id}/complete")
def activity_complete(activity_id: int, redirect_to: str = Form("/activities"), user: User = Depends(require_user), db: Session = Depends(get_db)):
    activity = _get_activity(activity_id, user, db)
    activity.completed = True
    activity.completed_at = datetime.now(timezone.utc)
    db.commit()
    return flash(RedirectResponse(redirect_to, 303), "Marked complete.")


@router.post("/{activity_id}/delete")
def activity_delete(activity_id: int, redirect_to: str = Form("/activities"), user: User = Depends(require_user), db: Session = Depends(get_db)):
    activity = _get_activity(activity_id, user, db)
    db.delete(activity); db.commit()
    return flash(RedirectResponse(redirect_to, 303), "Activity deleted.", "error")


def _get_activity(activity_id: int, user: User, db: Session) -> Activity:
    q = db.query(Activity).filter(Activity.id == activity_id)
    if not user.is_manager:
        q = q.filter(Activity.created_by_id == user.id)
    a = q.first()
    if not a:
        raise HTTPException(404, "Activity not found")
    return a

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import User, Activity, Deal
from app.models.activity import ActivityType
from app.templating import templates

router = APIRouter()


@router.get("")
def activities_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = db.query(Activity)
    if not user.is_manager:
        q = q.filter(Activity.created_by_id == user.id)
    overdue = q.filter(Activity.completed == False, Activity.due_at < datetime.now(timezone.utc), Activity.due_at.isnot(None)).order_by(Activity.due_at).all()
    upcoming = q.filter(Activity.completed == False, Activity.due_at >= datetime.now(timezone.utc)).order_by(Activity.due_at).limit(20).all()
    recent = q.filter(Activity.completed == True).order_by(Activity.created_at.desc()).limit(20).all()
    return templates.TemplateResponse(request, "activities/list.html", {
        "user": user, "flash": get_flash(request),
        "overdue": overdue, "upcoming": upcoming, "recent": recent,
        "activity_types": list(ActivityType),
    })


@router.post("/new")
def activity_create(
    request: Request,
    type_: str = Form(...), subject: str = Form(...), body: str = Form(""),
    deal_id: int = Form(None), due_at: str = Form(""),
    redirect_to: str = Form("/activities"),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    due = datetime.strptime(due_at, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) if due_at else None
    activity = Activity(
        type=ActivityType(type_), subject=subject, body=body or None,
        deal_id=deal_id or None, due_at=due, created_by_id=user.id,
    )
    db.add(activity)
    if deal_id:
        deal = db.get(Deal, deal_id)
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

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.models import User
from app.models.notification import Notification
from app.templating import templates

router = APIRouter()


@router.get("/unread-count")
def unread_count(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Returns an HTML badge element — consumed by HTMX hx-swap in the navbar."""
    from fastapi.responses import HTMLResponse
    count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.is_read == False)
        .count()
    )
    if count:
        html = (
            f'<span id="notif-badge" '
            f'hx-get="/notifications/unread-count" hx-trigger="every 60s" hx-swap="outerHTML" '
            f'class="absolute -top-0.5 -right-0.5 flex items-center justify-center '
            f'w-4 h-4 rounded-full bg-red-500 text-white text-[9px] font-bold leading-none">'
            f'{min(count, 99)}</span>'
        )
    else:
        html = (
            '<span id="notif-badge" '
            'hx-get="/notifications/unread-count" hx-trigger="every 60s" hx-swap="outerHTML" '
            'class="hidden"></span>'
        )
    return HTMLResponse(html)


@router.get("")
def list_notifications(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(request, "notifications.html", {
        "user": user,
        "notifications": notifications,
    })


@router.post("/mark-all-read")
def mark_all_read(user: User = Depends(require_user), db: Session = Depends(get_db)):
    db.query(Notification).filter(
        Notification.user_id == user.id, Notification.is_read == False
    ).update({"is_read": True}, synchronize_session=False)
    db.commit()
    return RedirectResponse("/notifications", 303)


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    n = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == user.id
    ).first()
    if n:
        n.is_read = True
        db.commit()
    redirect = n.link if n and n.link else "/notifications"
    return RedirectResponse(redirect, 303)

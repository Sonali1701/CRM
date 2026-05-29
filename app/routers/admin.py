"""
Admin dashboard — view audit logs and team performance metrics.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.models.audit_log import AuditLog
from app.deps import require_admin
from app.services.audit import get_team_activity
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/audit-logs")
def audit_logs_page(
    request: Request,
    days: int = 30,
    action: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Display audit logs for the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = db.query(AuditLog).filter(AuditLog.created_at >= cutoff)

    if action:
        query = query.filter(AuditLog.action == action)

    logs = query.order_by(AuditLog.created_at.desc()).limit(500).all()
    return templates.TemplateResponse(request, "admin/audit_logs.html", {
        "user": user,
        "logs": logs,
        "days": days,
        "total": len(logs),
    })


@router.get("/api/audit-logs")
async def audit_logs_api(
    days: int = 30,
    action: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Get audit logs JSON for the last N days, optionally filtered by action."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = db.query(AuditLog).filter(AuditLog.created_at >= cutoff)

    if action:
        query = query.filter(AuditLog.action == action)

    logs = query.order_by(AuditLog.created_at.desc()).limit(500).all()
    return {
        "total": len(logs),
        "logs": [
            {
                "id": log.id,
                "user": log.user.full_name if log.user else "Unknown",
                "action": log.action,
                "entity": log.entity_type,
                "entity_id": log.entity_id,
                "details": log.details,
                "created_at": log.created_at.isoformat(),
                "summary": log.summary,
            }
            for log in logs
        ],
    }


@router.get("/team-activity")
async def team_activity(
    days: int = 30,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Get team activity summary (counts by user and action)."""
    return get_team_activity(db, days)

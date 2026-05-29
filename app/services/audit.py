"""
Audit logging service — track user actions for admin visibility.
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models import User


def log_action(
    db: Session,
    user: User,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    details: dict[str, Any] | None = None,
):
    """
    Log a user action to the audit log.

    Args:
        db: Database session
        user: User performing the action
        action: Action type (e.g., "create_lead", "import_leads", "update_notes")
        entity_type: Type of entity (e.g., "lead", "import")
        entity_id: ID of the entity affected
        details: Additional details as a dict (count, source, names, etc.)
    """
    log = AuditLog(
        user_id=user.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
        created_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()


def get_team_activity(db: Session, days: int = 30) -> dict:
    """
    Get team activity summary for the last N days.
    Returns counts by action type and user.
    """
    from datetime import timedelta
    from sqlalchemy import func

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    logs = db.query(AuditLog).filter(AuditLog.created_at >= cutoff).all()

    # Group by user and action
    by_user = {}
    by_action = {}

    for log in logs:
        user_name = log.user.full_name if log.user else "Unknown"
        if user_name not in by_user:
            by_user[user_name] = {}
        by_user[user_name][log.action] = by_user[user_name].get(log.action, 0) + 1

        if log.action not in by_action:
            by_action[log.action] = 0
        by_action[log.action] += 1

    return {
        "by_user": by_user,
        "by_action": by_action,
        "total": len(logs),
    }

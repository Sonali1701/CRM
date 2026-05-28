"""
Maintain cached fields on Lead model (next_follow_up_at, etc).
Called after activity changes, syncs, etc.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.lead import Lead
from app.models.activity import Activity, ActivityType


def update_next_follow_up_cache(db: Session, lead_id: int):
    """
    Sync Lead.next_follow_up_at with the earliest open TASK due date.
    Call this whenever a TASK is created, completed, or modified.
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        return

    # Get the next open task (if any)
    next_task = db.query(Activity).filter(
        Activity.lead_id == lead_id,
        Activity.type == ActivityType.TASK,
        Activity.completed == False,  # noqa: E712
        Activity.due_at > datetime.now(timezone.utc),
    ).order_by(Activity.due_at).first()

    lead.next_follow_up_at = next_task.due_at if next_task else None
    db.commit()


def get_latest_activity_note(db: Session, lead_id: int, max_length: int = 120) -> str | None:
    """Get the most recent NOTE activity for a lead (preview text)."""
    latest = db.query(Activity).filter(
        Activity.lead_id == lead_id,
        Activity.type == ActivityType.NOTE,
    ).order_by(Activity.completed_at.desc().nulls_last(), Activity.created_at.desc()).first()

    if not latest:
        return None
    text = latest.body or latest.subject or ""
    return (text[:max_length] + "...") if len(text) > max_length else text

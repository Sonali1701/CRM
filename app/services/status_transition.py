"""
Auto-transition lead status based on activities.

When an activity is created for a lead:
- If lead is "new" → mark as "contacted"
- Keep "qualified", "converted", "disqualified" as manual
"""
from sqlalchemy.orm import Session

from app.models.lead import Lead, LeadStatus
from app.models.activity import Activity


def auto_transition_on_activity(db: Session, lead_id: int):
    """
    When an activity is created, auto-mark lead as "contacted" if still "new".
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        return

    # Only auto-transition from "new" to "contacted"
    if lead.status == LeadStatus.NEW:
        # Check if there's at least one activity for this lead
        has_activity = db.query(Activity).filter(Activity.lead_id == lead_id).first()
        if has_activity:
            lead.status = LeadStatus.CONTACTED
            db.commit()

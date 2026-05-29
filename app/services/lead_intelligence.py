"""
Auto-classify lead outreach notes using NLP and transition status based on engagement.

This runs when:
- Notes are updated (Excel sync, manual edit)
- Activities are created (email sent, follow-up logged)

Classifies using lightweight NLP (no AI quota), then auto-transitions status:
- "positive" → "qualified" (active interest, next step agreed)
- "reconnect_later" → stays "contacted" (they want to be pinged later)
- "decline", "in_house_only" → "disqualified" (explicit decline)
- Others → stays in current status
"""
import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.lead import Lead, LeadStatus
from app.services.nlp_classifier import classify_notes


def _hash_notes(text: str | None) -> str:
    """SHA1 hash of notes (first 16 chars) to detect changes."""
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16] if text else ""


def auto_transition_on_classification(db: Session, lead_id: int):
    """
    Auto-transition status based on existing outreach classification.
    This is called after AI classification runs (via /ai-tools/outreach or background job).
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead or not lead.outreach_category:
        return

    category = lead.outreach_category

    # Auto-transition based on engagement signal
    if category == "positive":
        # Active interest / next step agreed → qualified
        if lead.status in (LeadStatus.NEW, LeadStatus.CONTACTED):
            lead.status = LeadStatus.QUALIFIED
    elif category in ("not_interested_now", "in_house_only", "wrong_poc"):
        # Explicit decline → disqualified
        if lead.status not in (LeadStatus.CONVERTED, LeadStatus.DISQUALIFIED):
            lead.status = LeadStatus.DISQUALIFIED
    elif category == "reconnect_later":
        # They want to be pinged later — mark as contacted if still new
        if lead.status == LeadStatus.NEW:
            lead.status = LeadStatus.CONTACTED
    # else: info_sent, out_of_org, no_outcome → keep current status

    db.commit()


def classify_and_transition(db: Session, lead: Lead, notes: str):
    """
    Classify notes using NLP, update lead's outreach_category,
    and auto-transition status.
    Call this when notes are updated (sync, manual edit, etc).
    """
    if not notes or not notes.strip():
        return

    category = classify_notes(notes)
    lead.outreach_category = category
    lead.updated_at = datetime.now(timezone.utc)

    # Auto-transition based on classification
    auto_transition_on_classification(db, lead.id)


def get_engagement_score(lead: Lead) -> int:
    """
    Calculate 0-100 engagement score based on lead data.
    Used for sorting, filtering, or dashboards.
    """
    score = 0

    # Status progression (0-40 points)
    status_scores = {
        LeadStatus.NEW: 0,
        LeadStatus.CONTACTED: 15,
        LeadStatus.QUALIFIED: 30,
        LeadStatus.CONVERTED: 40,
        LeadStatus.DISQUALIFIED: 0,
    }
    score += status_scores.get(lead.status, 0)

    # Outreach classification (0-30 points)
    category_scores = {
        "positive": 30,
        "reconnect_later": 20,
        "info_sent": 10,
        "no_outcome": 5,
        "not_interested_now": 0,
        "in_house_only": 0,
        "wrong_poc": 5,
        "out_of_org": 0,
    }
    score += category_scores.get(lead.outreach_category, 0)

    # Recency bonus (0-30 points)
    if lead.updated_at:
        days_since = (datetime.now(timezone.utc) - lead.updated_at).days
        if days_since == 0:
            score += 30  # Updated today
        elif days_since <= 7:
            score += 20  # This week
        elif days_since <= 30:
            score += 10  # This month

    return min(score, 100)

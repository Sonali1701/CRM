"""Context builders for AI prompts.

The CompanyProfile gives every AI call an "about us" anchor (what we sell,
how we talk). The email-writer additionally gets two retrieval-augmented
blocks: recent saved templates (style reference) and the last few emails
sent to this contact (so the model doesn't repeat the same opening)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import CompanyProfile, EmailTemplate, Activity
from app.models.activity import ActivityType


def get_company_profile(db: Session) -> CompanyProfile:
    """Singleton — always returns the id=1 row, creating it if missing."""
    p = db.get(CompanyProfile, 1)
    if not p:
        p = CompanyProfile(id=1, name="Radixsol", website="https://radixsol.com")
        db.add(p)
        db.commit()
        db.refresh(p)
    return p


def build_company_block(profile: CompanyProfile | None) -> str:
    """Format the company profile as a block for the AI system prompt."""
    if not profile:
        return ""
    parts: list[str] = []
    if profile.name:
        parts.append(f"Company: {profile.name}")
    if profile.website:
        parts.append(f"Website: {profile.website}")
    if profile.tagline:
        parts.append(f"Tagline: {profile.tagline}")
    if profile.description:
        parts.append(f"What we do: {profile.description}")
    if profile.services:
        parts.append(f"Our services / offerings:\n{profile.services}")
    if profile.tone_guidelines:
        parts.append(f"Brand voice (follow when writing on our behalf):\n{profile.tone_guidelines}")
    if profile.website_excerpt:
        parts.append(f"Website excerpt (for additional grounding):\n{profile.website_excerpt[:2000]}")
    if profile.signature:
        parts.append(f"Email signature to use:\n{profile.signature}")
    if not parts:
        return ""
    return "## About us (use this to write in our voice and reference our services accurately)\n" + "\n\n".join(parts)


def build_email_writer_history(
    db: Session, lead_id: int | None,
    max_templates: int = 3, max_history: int = 3,
) -> str:
    """Pull recent templates + previous emails to this contact. Returns a
    block to append to the user-message of an email-writer call."""
    blocks: list[str] = []

    # Recent saved templates — style reference, not to be copied verbatim
    templates = (
        db.query(EmailTemplate)
        .order_by(EmailTemplate.updated_at.desc())
        .limit(max_templates)
        .all()
    )
    if templates:
        lines = []
        for t in templates:
            body_preview = (t.body or "").strip().replace("\r\n", "\n")[:400]
            lines.append(
                f"--- Template: {t.name} ---\n"
                f"Subject: {t.subject}\n"
                f"Body:\n{body_preview}"
            )
        blocks.append(
            "## Recent saved templates (study the tone and structure — do NOT copy them verbatim):\n"
            + "\n\n".join(lines)
        )

    # Previous emails sent TO this contact — avoids duplication
    if lead_id:
        history = (
            db.query(Activity)
            .filter(
                Activity.lead_id == lead_id,
                Activity.type == ActivityType.EMAIL,
            )
            .order_by(Activity.created_at.desc())
            .limit(max_history)
            .all()
        )
        if history:
            lines = []
            for a in history:
                body_preview = (a.body or "").strip()[:300]
                lines.append(
                    f"--- Sent {a.created_at.strftime('%Y-%m-%d')} ---\n"
                    f"Subject: {a.subject}\n"
                    f"Body: {body_preview}"
                )
            blocks.append(
                "## Previous emails already sent to this recipient "
                "(do NOT repeat the same opening or pitch — build on these):\n"
                + "\n\n".join(lines)
            )

    return "\n\n".join(blocks)

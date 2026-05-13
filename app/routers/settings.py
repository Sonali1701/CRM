"""Org-wide settings — admin only.

Currently houses just the Company Profile (used as RAG context in every AI
call). Future settings (custom field definitions, default email signatures
per role, etc.) can live here too."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_admin
from app.flash import flash, get_flash
from app.models import User
from app.services.ai_compose import fetch_company_context
from app.services.company_context import get_company_profile
from app.templating import templates


router = APIRouter()


@router.get("/company")
def company_profile_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    profile = get_company_profile(db)
    return templates.TemplateResponse(request, "settings/company.html", {
        "user": user, "flash": get_flash(request), "profile": profile,
    })


@router.post("/company")
async def company_profile_save(
    request: Request,
    name: str = Form(...),
    website: str = Form(""),
    tagline: str = Form(""),
    description: str = Form(""),
    services: str = Form(""),
    tone_guidelines: str = Form(""),
    signature: str = Form(""),
    refetch: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    profile = get_company_profile(db)
    website_changed = (website.strip() != (profile.website or "").strip())

    profile.name = name.strip() or "Radixsol"
    profile.website = website.strip()
    profile.tagline = tagline.strip()
    profile.description = description.strip()
    profile.services = services.strip()
    profile.tone_guidelines = tone_guidelines.strip()
    profile.signature = signature.strip()

    # Auto-refetch the website excerpt when URL changes, or when "Refetch" was clicked.
    msg = "Company profile saved."
    if profile.website and (website_changed or refetch):
        try:
            excerpt = await fetch_company_context(profile.website)
            if excerpt:
                profile.website_excerpt = excerpt
                profile.website_fetched_at = datetime.now(timezone.utc)
                msg = f"Saved. Cached {len(excerpt)} chars from your website for AI grounding."
            else:
                msg = "Saved. (Could not fetch the website — check the URL and try Refetch.)"
        except Exception as e:
            msg = f"Saved. Website refetch failed: {e}"

    db.commit()
    return flash(RedirectResponse("/settings/company", 303), msg)

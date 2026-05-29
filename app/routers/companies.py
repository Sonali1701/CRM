"""
Companies router — view company details and associated contacts.
"""
import logging
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import get_flash
from app.models import User, Client, Lead
from app.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("")
def companies_list(
    request: Request,
    search: str = "",
    sort: str = "newest",
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all companies (formal records + unique companies from contacts) with contact count."""
    logger.info(f"[COMPANIES] User {user.email} accessing companies list (page={page}, sort={sort}, search={search})")

    # Check total leads in database
    total_leads = db.query(Lead).count()
    logger.info(f"[COMPANIES] Total leads in database: {total_leads}")

    # Check leads with companies
    leads_with_company = db.query(Lead).filter(Lead.company.isnot(None)).count()
    logger.info(f"[COMPANIES] Leads with company field: {leads_with_company}")

    # Get all unique company names from leads (both formal and informal)
    unique_companies_query = db.query(
        Lead.company,
        Lead.client_id,
        func.count(Lead.id).label('contact_count')
    ).filter(
        Lead.company.isnot(None)
    ).group_by(Lead.company, Lead.client_id)

    if search:
        like = f"%{search}%"
        unique_companies_query = unique_companies_query.filter(Lead.company.ilike(like))

    # Apply sorting
    if sort == "contacts":
        unique_companies_query = unique_companies_query.order_by(func.count(Lead.id).desc())
    elif sort == "name":
        unique_companies_query = unique_companies_query.order_by(Lead.company)
    else:  # newest - order by first contact creation
        unique_companies_query = unique_companies_query.order_by(func.min(Lead.created_at).desc())

    # Get total count before pagination
    all_results = unique_companies_query.all()
    total = len(all_results)
    logger.info(f"[COMPANIES] Query returned {total} unique companies")

    # Pagination: 50 per page
    per_page = 50
    total_pages = (total + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    # Apply pagination
    companies = all_results[offset:offset + per_page]

    # Format results
    companies_data = []
    for company_name, client_id, contact_count in companies:
        company_obj = None
        if client_id:
            company_obj = db.query(Client).filter(Client.id == client_id).first()

        companies_data.append({
            "company": company_obj,
            "company_name": company_name,
            "industry": company_obj.industry if company_obj else None,
            "website": company_obj.website if company_obj else None,
            "is_formal": company_obj is not None,
            "contact_count": contact_count or 0,
        })

    logger.info(f"[COMPANIES] Rendering {len(companies_data)} companies on page {page}/{total_pages}")

    response_data = {
        "user": user,
        "flash": get_flash(request),
        "companies_data": companies_data,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "search": search,
        "sort": sort,
    }

    logger.info(f"[COMPANIES] Response data keys: {list(response_data.keys())}")
    logger.info(f"[COMPANIES] companies_data is None: {response_data['companies_data'] is None}")
    logger.info(f"[COMPANIES] companies_data length: {len(response_data['companies_data']) if response_data['companies_data'] else 0}")

    return templates.TemplateResponse(request, "companies/list.html", response_data)


@router.get("/{company_id}")
def company_detail(
    company_id: int,
    request: Request,
    search: str = "",
    sort: str = "newest",
    page: int = 1,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Show company details and associated contacts."""
    company = db.query(Client).filter(Client.id == company_id).first()
    if not company:
        from fastapi import HTTPException
        raise HTTPException(404, "Company not found")

    # Get contacts for this company
    q = db.query(Lead).filter(Lead.client_id == company_id)

    if search:
        like = f"%{search}%"
        q = q.filter(or_(Lead.first_name.ilike(like), Lead.last_name.ilike(like), Lead.email.ilike(like)))

    # Sort options
    if sort == "status":
        q = q.order_by(Lead.status, Lead.created_at.desc())
    elif sort == "engagement":
        from app.services.lead_intelligence import get_engagement_score
        # Sort by engagement score
        q = q.order_by(Lead.status.desc(), Lead.updated_at.desc().nulls_last())
    else:  # newest
        q = q.order_by(Lead.created_at.desc())

    # Pagination: 50 per page
    per_page = 50
    total = q.count()
    total_pages = (total + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    contacts = q.offset(offset).limit(per_page).all()

    # Get latest activity note for each contact
    from app.services.lead_cache import get_latest_activity_note
    contacts_data = []
    for contact in contacts:
        last_note = get_latest_activity_note(db, contact.id)
        contacts_data.append({
            "contact": contact,
            "last_note": last_note,
        })

    # Calculate company stats
    from app.models.lead import LeadStatus
    status_counts = db.query(Lead.status, func.count(Lead.id)).filter(
        Lead.client_id == company_id
    ).group_by(Lead.status).all()
    status_breakdown = {s.value: c for s, c in status_counts}

    return templates.TemplateResponse(request, "companies/detail.html", {
        "user": user,
        "flash": get_flash(request),
        "company": company,
        "contacts_data": contacts_data,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "status_breakdown": status_breakdown,
        "search": search,
        "sort": sort,
    })

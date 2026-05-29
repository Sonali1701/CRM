"""
Companies router — view company details and associated contacts.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import get_flash
from app.models import User, Client, Lead
from app.templating import templates

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
    from sqlalchemy import distinct, union_all

    # Get formal company records with their linked contact counts
    formal_companies = db.query(
        Client.id,
        Client.name,
        Client.industry,
        Client.website,
        Client.created_at,
        Client.owner_id,
        func.count(distinct(Lead.id)).label('contact_count'),
        func.cast(Client.id, db.Integer).label('client_id'),
        func.cast(1, db.Integer).label('is_formal')
    ).outerjoin(Lead, Lead.client_id == Client.id)

    if search:
        like = f"%{search}%"
        formal_companies = formal_companies.filter(
            or_(Client.name.ilike(like), Client.industry.ilike(like))
        )

    formal_companies = formal_companies.group_by(Client.id, Client.name, Client.industry, Client.website, Client.created_at, Client.owner_id)

    # Get informal companies (from Lead.company field) without a formal Client record
    informal_companies = db.query(
        func.cast(None, db.Integer).label('id'),
        Lead.company.label('name'),
        func.cast(None, db.String).label('industry'),
        func.cast(None, db.String).label('website'),
        func.cast(None, db.DateTime).label('created_at'),
        func.cast(None, db.Integer).label('owner_id'),
        func.count(distinct(Lead.id)).label('contact_count'),
        func.cast(None, db.Integer).label('client_id'),
        func.cast(0, db.Integer).label('is_formal')
    ).filter(
        Lead.company.isnot(None),
        Lead.client_id.is_(None)  # Only companies without formal Client records
    )

    if search:
        like = f"%{search}%"
        informal_companies = informal_companies.filter(Lead.company.ilike(like))

    informal_companies = informal_companies.group_by(Lead.company)

    # Combine both queries
    all_companies = formal_companies.union_all(informal_companies).subquery()

    # Query combined results
    q = db.query(all_companies)

    # Sort options
    if sort == "contacts":
        q = q.order_by(all_companies.c.contact_count.desc())
    elif sort == "name":
        q = q.order_by(all_companies.c.name)
    else:  # newest
        q = q.order_by(all_companies.c.created_at.desc().nulls_last())

    # Pagination: 50 per page
    per_page = 50
    total = q.count()
    total_pages = (total + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    companies = q.offset(offset).limit(per_page).all()

    # Format results
    companies_data = []
    for row in companies:
        company_obj = None
        if row.id:  # Formal company
            company_obj = db.query(Client).filter(Client.id == row.id).first()

        companies_data.append({
            "company": company_obj,
            "company_name": row.name,
            "industry": row.industry,
            "website": row.website,
            "is_formal": row.is_formal == 1,
            "contact_count": row.contact_count or 0,
        })

    return templates.TemplateResponse(request, "companies/list.html", {
        "user": user,
        "flash": get_flash(request),
        "companies_data": companies_data,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "search": search,
        "sort": sort,
    })


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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_user
from app.flash import flash, get_flash
from app.models import EmailTemplate, User
from app.templating import templates as jinja


router = APIRouter()


def _visible(db: Session, user: User):
    """User's own templates + everyone's shared templates."""
    return db.query(EmailTemplate).filter(
        or_(EmailTemplate.created_by_id == user.id, EmailTemplate.is_shared == True)
    )


@router.get("")
def list_templates(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    items = _visible(db, user).order_by(EmailTemplate.name).all()
    return jinja.TemplateResponse(request, "email_templates/list.html", {
        "user": user, "flash": get_flash(request), "templates": items,
    })


@router.get("/new")
def new_template(request: Request, user: User = Depends(require_user)):
    return jinja.TemplateResponse(request, "email_templates/form.html", {
        "user": user, "template": None,
    })


@router.post("/new")
def create_template(
    name: str = Form(...), subject: str = Form(...), body: str = Form(...),
    is_shared: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    t = EmailTemplate(
        name=name.strip(), subject=subject, body=body,
        is_shared=bool(is_shared), created_by_id=user.id,
    )
    db.add(t); db.commit()
    return flash(RedirectResponse("/email-templates", 303), "Template saved.")


@router.get("/{tid}")
def edit_template(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    t = _get(tid, user, db)
    return jinja.TemplateResponse(request, "email_templates/form.html", {
        "user": user, "template": t,
    })


@router.post("/{tid}")
def update_template(
    tid: int,
    name: str = Form(...), subject: str = Form(...), body: str = Form(...),
    is_shared: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    t = _get(tid, user, db)
    t.name = name.strip()
    t.subject = subject
    t.body = body
    t.is_shared = bool(is_shared)
    db.commit()
    return flash(RedirectResponse("/email-templates", 303), "Template updated.")


@router.post("/{tid}/delete")
def delete_template(tid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    t = _get(tid, user, db)
    if t.created_by_id != user.id and not user.is_admin:
        raise HTTPException(403, "Only the creator (or an admin) can delete this template")
    db.delete(t); db.commit()
    return flash(RedirectResponse("/email-templates", 303), "Template deleted.", "error")


def _get(tid: int, user: User, db: Session) -> EmailTemplate:
    t = db.query(EmailTemplate).filter(EmailTemplate.id == tid).first()
    if not t:
        raise HTTPException(404, "Template not found")
    if t.created_by_id != user.id and not t.is_shared:
        raise HTTPException(403)
    return t

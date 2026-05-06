from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user_optional
from app.models import User
from app.models.login_event import LoginEvent
from app.security import verify_password, create_session_token
from app.templating import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user_optional(request, db)
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "auth/login.html", {"user": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"user": None, "error": "Invalid email or password.", "email": email},
            status_code=400,
        )

    token = create_session_token(user.id)

    # Record login event
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "")
    ua = request.headers.get("user-agent", "")[:512]
    event = LoginEvent(
        user_id=user.id,
        ip_address=ip.split(",")[0].strip() if ip else None,
        user_agent=ua,
        session_token_prefix=token[:12],
    )
    db.add(event)
    db.commit()

    settings = get_settings()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        settings.session_cookie_name, token,
        max_age=settings.session_max_age_seconds,
        httponly=True, samesite="lax",
        secure=settings.env == "production",
    )
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name, "")
    if token:
        event = db.query(LoginEvent).filter(
            LoginEvent.session_token_prefix == token[:12]
        ).order_by(LoginEvent.logged_in_at.desc()).first()
        if event:
            db.delete(event)
            db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name)
    return response

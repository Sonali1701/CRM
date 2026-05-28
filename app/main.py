from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.deps import AuthRedirect
from app.flash import get_flash, clear_flash_response
from app.routers import (
    auth, leads, clients, deals, activities, dashboard, users, pipeline, imports, mail,
    email_templates, sequences, reports, cron, ai_tools, settings as settings_router,
    funnel, notifications,
)
from app.routers import auto_sync as auto_sync_router


def _ensure_admin_user():
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import User
    from app.models.user import UserRole
    from app.security import hash_password

    settings = get_settings()
    if not settings.admin_email or not settings.admin_password:
        return
    email = settings.admin_email.lower().strip()
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == email).first()
        if not admin:
            db.add(User(
                email=email,
                full_name=settings.admin_name,
                password_hash=hash_password(settings.admin_password),
                role=UserRole.ADMIN,
            ))
            db.commit()
            print(f"[startup] Admin user created: {email}")
        else:
            changed = False
            if admin.role != UserRole.ADMIN:
                admin.role = UserRole.ADMIN
                changed = True
            if not admin.is_active:
                admin.is_active = True
                changed = True
            if changed:
                db.commit()
                print(f"[startup] Admin user normalized: {email}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_admin_user()
    from app.services.sheet_sync import start_background_worker
    start_background_worker()
    yield


app = FastAPI(title="Radixsol CRM", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(leads.router, prefix="/leads")
app.include_router(clients.router, prefix="/clients")
app.include_router(deals.router, prefix="/deals")
app.include_router(activities.router, prefix="/activities")
app.include_router(dashboard.router)
app.include_router(users.router, prefix="/users")
app.include_router(pipeline.router)
app.include_router(funnel.router)
app.include_router(notifications.router, prefix="/notifications")
app.include_router(imports.router, prefix="/import")
app.include_router(mail.router, prefix="/mail")
app.include_router(email_templates.router, prefix="/email-templates")
app.include_router(sequences.router, prefix="/sequences")
app.include_router(reports.router)
app.include_router(cron.router)
app.include_router(ai_tools.router, prefix="/ai-tools")
app.include_router(settings_router.router, prefix="/settings")
app.include_router(auto_sync_router.router)


@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, _exc: AuthRedirect):
    return RedirectResponse("/login", status_code=303)


@app.middleware("http")
async def flash_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.cookies.get("flash_message"):
        clear_flash_response(response)
    return response

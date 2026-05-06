from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.deps import AuthRedirect
from app.flash import get_flash, clear_flash_response
from app.routers import auth, leads, clients, deals, activities, dashboard, users, pipeline, imports

app = FastAPI(title="Radixsol CRM")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(leads.router, prefix="/leads")
app.include_router(clients.router, prefix="/clients")
app.include_router(deals.router, prefix="/deals")
app.include_router(activities.router, prefix="/activities")
app.include_router(dashboard.router)
app.include_router(users.router, prefix="/users")
app.include_router(pipeline.router)
app.include_router(imports.router, prefix="/import")


@app.on_event("startup")
def auto_create_admin():
    """Create the first admin user on startup if ADMIN_EMAIL + ADMIN_PASSWORD are set and DB is empty."""
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import User
    from app.models.user import UserRole
    from app.security import hash_password

    settings = get_settings()
    if not settings.admin_email or not settings.admin_password:
        return

    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(
                email=settings.admin_email.lower().strip(),
                full_name=settings.admin_name,
                password_hash=hash_password(settings.admin_password),
                role=UserRole.ADMIN,
            )
            db.add(admin)
            db.commit()
            print(f"[startup] Admin user created: {settings.admin_email}")
        else:
            print(f"[startup] Users already exist — skipping auto-create.")
    finally:
        db.close()


@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, _exc: AuthRedirect):
    return RedirectResponse("/login", status_code=303)


@app.middleware("http")
async def flash_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.cookies.get("flash_message"):
        clear_flash_response(response)
    return response

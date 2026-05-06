from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_admin, require_user
from app.flash import flash, get_flash
from app.models import User
from app.models.user import UserRole
from app.security import hash_password
from app.templating import templates

router = APIRouter()


@router.get("")
def users_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.full_name).all()
    return templates.TemplateResponse(request, "users/list.html", {
        "user": user, "flash": get_flash(request), "users": users, "roles": list(UserRole),
    })


@router.post("/new")
def user_create(
    full_name: str = Form(...), email: str = Form(...),
    password: str = Form(...), role: str = Form("sales"),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    if db.query(User).filter(User.email == email.lower().strip()).first():
        raise HTTPException(400, "Email already exists")
    new_user = User(
        full_name=full_name, email=email.lower().strip(),
        password_hash=hash_password(password), role=UserRole(role),
    )
    db.add(new_user); db.commit()
    return flash(RedirectResponse("/users", 303), f"User {full_name} created.")


@router.post("/{user_id}/toggle")
def user_toggle(user_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.id == user.id:
        raise HTTPException(400, "Cannot deactivate yourself")
    target.is_active = not target.is_active
    db.commit()
    state = "activated" if target.is_active else "deactivated"
    return flash(RedirectResponse("/users", 303), f"User {state}.")


@router.post("/{user_id}/reset-password")
def user_reset_password(
    user_id: int, new_password: str = Form(...),
    user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    target.password_hash = hash_password(new_password)
    db.commit()
    return flash(RedirectResponse("/users", 303), "Password reset.")


@router.get("/profile")
def profile(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "users/profile.html", {
        "user": user, "flash": get_flash(request),
    })


@router.post("/profile")
def profile_update(
    request: Request,
    full_name: str = Form(...), current_password: str = Form(""), new_password: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from app.security import verify_password
    user.full_name = full_name
    if new_password:
        if not verify_password(current_password, user.password_hash):
            return templates.TemplateResponse(request, "users/profile.html", {
                "user": user, "error": "Current password is incorrect.",
            }, status_code=400)
        user.password_hash = hash_password(new_password)
    db.commit()
    return flash(RedirectResponse("/users/profile", 303), "Profile updated.")

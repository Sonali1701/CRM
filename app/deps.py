from fastapi import Depends, Request, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User, UserRole
from app.security import read_session_token


class AuthRedirect(Exception):
    """Raised when an unauthenticated user hits a protected page; handled in main.py."""


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(get_settings().session_cookie_name)
    user_id = read_session_token(token) if token else None
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user_optional(request, db)
    if not user or not user.is_active:
        raise AuthRedirect()
    return user


def require_manager(user: User = Depends(require_user)) -> User:
    if not user.is_manager:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager role required")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user

import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from . import models
from .config import ADMIN_PASSWORD, ADMIN_USERNAME
from .database import get_db

# auto_error=False + no WWW-Authenticate header: the browser must not pop up
# its native login dialog, the dashboard has its own login form.
_basic = HTTPBasic(auto_error=False)


def _credentials_ok(credentials: Optional[HTTPBasicCredentials]) -> bool:
    if credentials is None:
        return False
    user_ok = secrets.compare_digest(
        credentials.username.encode(), ADMIN_USERNAME.encode()
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode(), ADMIN_PASSWORD.encode()
    )
    return user_ok and pass_ok


def require_admin(credentials: Optional[HTTPBasicCredentials] = Depends(_basic)):
    if not _credentials_ok(credentials):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")


def require_admin_browser(
    credentials: Optional[HTTPBasicCredentials] = Depends(_basic),
):
    """Same check, but asks the browser to show its native login prompt.

    Only for pages opened directly in the browser (the API docs); the dashboard
    uses require_admin so it keeps its own login form.
    """
    if not _credentials_ok(credentials):
        raise HTTPException(
            status_code=401,
            detail="Admin login required",
            headers={"WWW-Authenticate": 'Basic realm="20Seconds admin"'},
        )


def find_user(db: Session, request: Request, token: str) -> Optional[models.User]:
    """Resolve a user token (uuid) to a user, or None if it is not valid."""
    user = (
        db.query(models.User)
        .filter(models.User.uuid == token.strip().lower())
        .first()
    )
    if user is not None:
        # Picked up by the request-log middleware.
        request.scope["user_id"] = user.user_id
    return user

"""FastAPI dependency: resolve Bearer token → authenticated User row."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from saas_mvp.auth.security import PyJWTError, decode_access_token
from saas_mvp.db import get_db
from saas_mvp.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Extract & validate JWT, return the matching User or raise 401."""
    try:
        payload = decode_access_token(token)
        user_id_str: str | None = payload.get("sub")
        if not user_id_str:
            raise _401
        user_id = int(user_id_str)
    except (PyJWTError, ValueError):
        raise _401

    user = db.get(User, user_id)
    if user is None:
        raise _401
    return user

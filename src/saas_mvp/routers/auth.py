"""Auth router: register, login (token), whoami."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import get_current_user
from saas_mvp.auth.security import create_access_token, hash_password, verify_password
from saas_mvp.db import get_db
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


# ────────────────────────────── Schemas ───────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_name: str  # tenant is created on-the-fly if it does not exist


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserInfo(BaseModel):
    id: int
    email: str
    tenant_id: int
    tenant_name: str

    model_config = {"from_attributes": True}


# ────────────────────────────── Endpoints ─────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Create a new user (and tenant if needed). Returns a JWT immediately."""
    # duplicate e-mail check
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # get-or-create tenant
    tenant = db.query(Tenant).filter(Tenant.name == body.tenant_name).first()
    if not tenant:
        tenant = Tenant(name=body.tenant_name, plan="free")
        db.add(tenant)
        db.flush()  # populate tenant.id before we reference it

    # create user — password stored as bcrypt hash only
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        tenant_id=tenant.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token)


@router.post("/token", response_model=TokenResponse)
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """OAuth2-compatible form login (username = email). Returns a JWT."""
    user = db.query(User).filter(User.email == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserInfo)
def whoami(current_user: User = Depends(get_current_user)) -> UserInfo:
    """Return the authenticated user's profile."""
    return UserInfo(
        id=current_user.id,
        email=current_user.email,
        tenant_id=current_user.tenant_id,
        tenant_name=current_user.tenant.name,
    )

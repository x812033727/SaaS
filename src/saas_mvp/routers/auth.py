"""Auth router: register, login (token), whoami.

Security hardening:
- Password minimum length enforced at schema level (Field min_length=8).
- Tenant name is exclusive to its creator — joining an existing tenant is rejected
  (prevents unauthorized multi-tenancy via name-guessing).
- /register and /token are rate-limited to prevent brute-force / credential stuffing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import get_current_user, oauth2_scheme
from saas_mvp.auth.ratelimit import register_limiter, token_limiter
from saas_mvp.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from saas_mvp.config import settings
from saas_mvp.db import get_db
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services import login_audit
from saas_mvp.services import organizations as organizations_svc
from saas_mvp.services.mailer import Mailer, get_mailer

router = APIRouter(prefix="/auth", tags=["auth"])


# ────────────────────────────── Schemas ───────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    # min_length=8 blocks blank / trivially short passwords
    password: str = Field(min_length=8, description="At least 8 characters")
    tenant_name: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    # 與註冊相同的最低強度要求
    new_password: str = Field(min_length=8, description="At least 8 characters")


class UserInfo(BaseModel):
    id: int
    email: str
    tenant_id: int
    tenant_name: str

    model_config = {"from_attributes": True}


# ────────────────────────────── Endpoints ─────────────────────────────────────

@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(register_limiter)],
)
def register(
    body: RegisterRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Create a new user in a *new* tenant.

    Attempting to register under an existing tenant name returns 400 —
    tenant membership is invite-only (not implemented in this iteration).
    """
    # Duplicate e-mail guard
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Tenant exclusivity: reject if the name is already taken
    if db.query(Tenant).filter(Tenant.name == body.tenant_name).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant name already taken. Use a unique tenant name.",
        )

    organization = organizations_svc.create_organization(
        db, name=body.tenant_name, flush=True
    )
    tenant = Tenant(
        name=body.tenant_name, plan="free", organization_id=organization.id
    )
    db.add(tenant)
    db.flush()  # populate tenant.id before we reference it

    # Store bcrypt hash only — never the plain-text value
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        tenant_id=tenant.id,
    )
    db.add(user)
    db.flush()
    organizations_svc.add_owner_memberships(
        db, organization_id=organization.id, tenant_id=tenant.id, user_id=user.id
    )
    db.commit()
    db.refresh(user)

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token)


@router.post(
    "/token",
    response_model=TokenResponse,
    dependencies=[Depends(token_limiter)],
)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    mailer: Mailer = Depends(get_mailer),
) -> TokenResponse:
    """OAuth2-compatible form login (username = email). Returns a JWT."""
    user = db.query(User).filter(User.email == form.username).first()
    # Unified 401 regardless of whether the user exists or the password is wrong
    # (prevents user-enumeration via timing / error messages)
    if not user or not verify_password(form.password, user.hashed_password):
        login_audit.on_login_failure(db, email=form.username, request=request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    login_audit.on_login_success(db, user, request, mailer=mailer)
    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token)


@router.post(
    "/renew",
    response_model=TokenResponse,
    dependencies=[Depends(token_limiter)],
)
def renew_token(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """滑動續期(R4-C1):以**仍有效**的 token 換一顆新 token。

    安全邊界:
    * 過期票 decode 即拋 → 不可能續過期票。
    * ``imp`` 代管票一律 403(30 分硬上限是刻意設計,不可展延)。
    * ``oa``(首次登入時間)超過 ``session_renew_max_hours`` → 401 強制重登;
      新票原樣攜帶 oa,滑動視窗有總長上限。
    * 使用者已停用/刪除 → 401。
    """
    import datetime as _dt

    import jwt as _jwt

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")
    try:
        payload = decode_access_token(token)
    except _jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token")
    if payload.get("imp") is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Impersonation tokens cannot be renewed")
    now_ts = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    oa = int(payload.get("oa") or now_ts)
    if now_ts - oa > settings.session_renew_max_hours * 3600:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Session too old; please log in again")
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User not found")
    new_token = create_access_token(
        user_id=user.id, tenant_id=payload["tenant_id"], original_auth_ts=oa
    )
    return TokenResponse(access_token=new_token)


@router.get("/me", response_model=UserInfo)
def whoami(current_user: User = Depends(get_current_user)) -> UserInfo:
    """Return the authenticated user's profile."""
    return UserInfo(
        id=current_user.id,
        email=current_user.email,
        tenant_id=current_user.tenant_id,
        tenant_name=current_user.tenant.name,
    )


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    # 與登入同級的 per-IP 限流：擋線上密碼猜測 / 濫用
    dependencies=[Depends(token_limiter)],
)
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """變更登入密碼：須通過目前密碼驗證；新密碼至少 8 字元且不得與目前相同。

    成功回 204。current_user 由請求 session 解析，與此處注入的 db 為同一 session，
    故直接更新並 commit 即生效（既有 JWT/cookie 不會被動失效——屬已知行為）。
    """
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    if verify_password(body.new_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current one",
        )
    current_user.hashed_password = hash_password(body.new_password)
    db.add(current_user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

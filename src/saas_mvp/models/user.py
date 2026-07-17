"""User model."""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(256), unique=True, nullable=False, index=True)
    hashed_password = Column(String(256), nullable=False)  # bcrypt hash only — no plaintext
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    # Email 驗證（B3）：NULL = 未驗證。未驗證僅 banner 提醒不硬擋；
    # trial 轉付費（訂閱方案）前必須驗證。Alembic rev 0008 補欄。
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    # 店內角色（B5）：owner（帳務/LINE 設定/成員管理）| staff（日常營運）。
    # 與 is_admin（平台管理員，跨租戶）是兩個維度。Alembic rev 0011 補欄。
    role = Column(String(16), nullable=False, default="owner", server_default="owner")

    # OAuth 登入（LINE Login / Google）外部身分。皆 nullable：密碼註冊用戶為 NULL。
    # 既有 DB 由 db._migrate_add_user_oauth() 補欄。oauth_subject 為 provider 端的
    # 穩定使用者 ID；以 email 不分大小寫做帳號連結（見 services/oauth.py）。
    oauth_provider = Column(String(16), nullable=True)
    oauth_subject = Column(String(128), nullable=True)

    # 登入稽核（R5-D1）：上次成功登入時間 / IP。IP 供「新位置登入」啟發式
    # 比對用（本次 ≠ 上次 → email 通知）。Alembic rev 0051 補欄。
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_login_ip = Column(String(64), nullable=True)

    # TOTP 2FA（R5-D2）：secret Fernet 加密存放；totp_enabled_at 為 NULL 時
    # 代表未啟用（secret 可能是註冊到一半的暫存值，不生效）。Alembic rev 0052。
    totp_secret_enc = Column(LargeBinary, nullable=True)
    totp_enabled_at = Column(DateTime(timezone=True), nullable=True)

    # 工作階段撤銷（R5-D3）：JWT 帶 `tv` claim，decode 時與此比對，不符即失效。
    # 改密碼/停用成員/「登出所有裝置」= token_version+1。舊票（無 tv）視為 0，
    # 部署當下既有票在 token_version 仍為 0 的 user 上繼續有效（零中斷）。rev 0053。
    token_version = Column(Integer, nullable=False, default=0, server_default="0")
    # 成員停用（R5-D3）：非 NULL = 已停用，登入擋 + decode 每請求重載即刻失效。
    disabled_at = Column(DateTime(timezone=True), nullable=True)

    tenant = relationship("Tenant", back_populates="users")
    organization_memberships = relationship(
        "OrganizationMember", back_populates="user", cascade="all, delete-orphan"
    )
    tenant_memberships = relationship(
        "TenantMember", back_populates="user", cascade="all, delete-orphan"
    )
    location_memberships = relationship(
        "LocationMember", back_populates="user", cascade="all, delete-orphan"
    )
    notes = relationship("Note", back_populates="owner", cascade="all, delete-orphan")
    api_keys = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")

    @property
    def totp_secret(self) -> str:
        return decrypt_field(self.totp_secret_enc) if self.totp_secret_enc else ""

    @totp_secret.setter
    def totp_secret(self, value: str) -> None:
        self.totp_secret_enc = encrypt_field(value) if value else None

    @property
    def totp_enabled(self) -> bool:
        """2FA 生效 = 已確認啟用且 secret 仍在。"""
        return self.totp_enabled_at is not None and self.totp_secret_enc is not None

"""User model."""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


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

    # OAuth 登入（LINE Login / Google）外部身分。皆 nullable：密碼註冊用戶為 NULL。
    # 既有 DB 由 db._migrate_add_user_oauth() 補欄。oauth_subject 為 provider 端的
    # 穩定使用者 ID；以 email 不分大小寫做帳號連結（見 services/oauth.py）。
    oauth_provider = Column(String(16), nullable=True)
    oauth_subject = Column(String(128), nullable=True)

    tenant = relationship("Tenant", back_populates="users")
    notes = relationship("Note", back_populates="owner", cascade="all, delete-orphan")
    api_keys = relationship("ApiKey", back_populates="user", cascade="all, delete-orphan")

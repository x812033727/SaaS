"""LineChannelConfig model — 每租戶 LINE channel 設定，一對一。

channel_secret 與 access_token 以 Fernet 對稱加密存 DB（可逆還原，
供 HMAC 驗章與 reply API 使用），不以明文儲存。

加密金鑰來源：SAAS_LINE_CHANNEL_ENCRYPT_KEY（44 字元 URL-safe base64）。
測試環境使用 config.py 的 dev 預設值即可離線跑。
"""

from __future__ import annotations

import datetime

from cryptography.fernet import Fernet
from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import relationship

from saas_mvp.db import Base


# ── 加密工具 ────────────────────────────────────────────────────────────────

def _get_fernet() -> Fernet:
    """每次取用時重新建立（允許測試修改 settings 後立即生效）。"""
    from saas_mvp.config import settings
    return Fernet(settings.line_channel_encrypt_key.encode())


def encrypt_field(value: str) -> bytes:
    """將明文字串 Fernet 加密後回傳 bytes。"""
    return _get_fernet().encrypt(value.encode())


def decrypt_field(data: bytes) -> str:
    """將 Fernet 加密 bytes 解密後回傳明文字串。"""
    return _get_fernet().decrypt(data).decode()


# ── ORM Model ───────────────────────────────────────────────────────────────

class LineChannelConfig(Base):
    __tablename__ = "line_channel_configs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id"),
        nullable=False,
        unique=True,   # 一對一
        index=True,
    )

    # 加密欄位：儲存 Fernet ciphertext（bytes）
    channel_secret_enc = Column(LargeBinary, nullable=False)
    access_token_enc = Column(LargeBinary, nullable=False)

    # 預設翻譯目標語言（BCP-47 tag，如 "zh-TW", "en", "ja"）
    default_target_lang = Column(String(16), nullable=False, default="zh-TW")

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    tenant = relationship("Tenant", back_populates="line_channel_config")

    # ── 便利屬性：透明加解密 ────────────────────────────────────────────────

    @property
    def channel_secret(self) -> str:
        """解密後回傳 channel secret 明文。"""
        return decrypt_field(self.channel_secret_enc)

    @channel_secret.setter
    def channel_secret(self, value: str) -> None:
        """加密並存入 channel_secret_enc。"""
        self.channel_secret_enc = encrypt_field(value)

    @property
    def access_token(self) -> str:
        """解密後回傳 access token 明文。"""
        return decrypt_field(self.access_token_enc)

    @access_token.setter
    def access_token(self, value: str) -> None:
        """加密並存入 access_token_enc。"""
        self.access_token_enc = encrypt_field(value)

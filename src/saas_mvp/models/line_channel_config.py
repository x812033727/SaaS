"""LineChannelConfig model — 每租戶 LINE channel 設定，一對一。

channel_secret 與 access_token 以 Fernet 對稱加密存 DB（可逆還原，
供 HMAC 驗章與 reply API 使用），不以明文儲存。

加密金鑰來源：SAAS_LINE_CHANNEL_ENCRYPT_KEY（44 字元 URL-safe base64）。
測試環境使用 config.py 的 dev 預設值即可離線跑。
"""

from __future__ import annotations

import datetime
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import relationship, validates

from saas_mvp.db import Base

# BCP-47 tag 基本格式（language[-script][-region][-variant...]），
# 僅允許合法字元，防止下游 API 注入。
_BCP47_RE = re.compile(r"^[a-zA-Z]{2,8}(-[a-zA-Z0-9]{2,8})*$")


class LineConfigDecryptionError(RuntimeError):
    """金鑰輪換或資料損壞導致 Fernet 解密失敗。"""


class InvalidTargetLangError(ValueError):
    """`default_target_lang` 不符合 BCP-47 格式。"""


# ── 加密工具 ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def _get_fernet_cached(key_bytes: bytes) -> Fernet:
    """依 key bytes 快取 Fernet 實例；key 輪換時不同 bytes → 自動建新實例。"""
    return Fernet(key_bytes)


def _get_fernet() -> Fernet:
    """取得（快取）Fernet 實例。key 改變時 lru_cache 自動建新實例。"""
    from saas_mvp.config import settings
    return _get_fernet_cached(settings.line_channel_encrypt_key.encode())


def encrypt_field(value: str) -> bytes:
    """將明文字串 Fernet 加密後回傳 bytes。"""
    return _get_fernet().encrypt(value.encode())


def decrypt_field(data: bytes) -> str:
    """將 Fernet 加密 bytes 解密後回傳明文字串。

    捕捉 InvalidToken（金鑰輪換或資料損壞），轉為 LineConfigDecryptionError
    方便上層診斷，而非直接拋 500。
    """
    try:
        return _get_fernet().decrypt(data).decode()
    except InvalidToken as exc:
        raise LineConfigDecryptionError(
            "Failed to decrypt LINE channel config field. "
            "The encryption key may have been rotated or the data is corrupted."
        ) from exc


def validate_target_lang(lang: str) -> str:
    """驗證 BCP-47 格式並回傳原字串；格式不合拋 InvalidTargetLangError。

    允許: "en", "zh-TW", "zh-Hant-TW", "ja"
    拒絕: 空字串、含空格、注入字元
    """
    if not _BCP47_RE.match(lang):
        raise InvalidTargetLangError(
            f"Invalid target language tag: {lang!r}. "
            "Must match BCP-47 format, e.g. 'en', 'zh-TW', 'ja'."
        )
    return lang


# ── ORM Model ───────────────────────────────────────────────────────────────

class LineChannelConfig(Base):
    __tablename__ = "line_channel_configs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),  # 裸 SQL 刪 tenant 也清孤兒行
        nullable=False,
        unique=True,   # 一對一
        index=True,
    )

    # 加密欄位：儲存 Fernet ciphertext（bytes）
    channel_secret_enc = Column(LargeBinary, nullable=False)
    access_token_enc = Column(LargeBinary, nullable=False)

    # LINE bot 的 userId（webhook payload.destination 比對用，作租戶識別二次驗證）。
    # nullable：舊資料/未取得 bot/info 時為 NULL，向後相容；unique 防同一 bot 跨租戶誤配。
    # 新環境由 create_all 直接建立；既有 DB 由 db._migrate_add_line_bot_user_id() 補欄。
    line_bot_user_id = Column(String(64), nullable=True, unique=True, index=True)

    # 預設翻譯目標語言（BCP-47 tag，如 "zh-TW", "en", "ja"）
    default_target_lang = Column(String(16), nullable=False, default="zh-TW")

    # LINE bot 的 userId（payload.destination 比對用，格式 U[0-9a-f]{32}）。
    # nullable：舊資料/尚未經 bot/info 回填的 config 為 NULL，向後相容。
    # unique：同一 bot 不應對應多租戶；SQLite 允許多個 NULL，遷移前無重複實值疑慮。
    line_bot_user_id = Column(String(64), nullable=True, unique=True, index=True)

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

    # ── ORM 層驗證：直接賦值也會觸發 ────────────────────────────────────────

    @validates("default_target_lang")
    def _validate_lang(self, key: str, value: str) -> str:
        """BCP-47 格式強制——setter/constructor 賦值皆觸發。"""
        return validate_target_lang(value)

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

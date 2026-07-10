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
from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import relationship, validates

from saas_mvp.db import Base

# BCP-47 tag 基本格式（language[-script][-region][-variant...]），
# 僅允許合法字元，防止下游 API 注入。
_BCP47_RE = re.compile(r"^[a-zA-Z]{2,8}(-[a-zA-Z0-9]{2,8})*$")

# bot 行為模式：translation（現狀翻譯，預設）/ booking（預約）/ auto_reply（關鍵字自動回覆）。
# 並存設計——webhook 依此值分流，既有翻譯店家不受影響。
VALID_BOT_MODES: frozenset[str] = frozenset({"translation", "booking", "auto_reply"})
DEFAULT_BOT_MODE = "translation"


class LineConfigDecryptionError(RuntimeError):
    """金鑰輪換或資料損壞導致 Fernet 解密失敗。"""


class InvalidTargetLangError(ValueError):
    """`default_target_lang` 不符合 BCP-47 格式。"""


class InvalidBotModeError(ValueError):
    """`bot_mode` 不在 VALID_BOT_MODES。"""


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


def validate_bot_mode(value: str) -> str:
    """驗證 bot_mode 並回傳原字串；不在白名單拋 InvalidBotModeError。"""
    if value not in VALID_BOT_MODES:
        raise InvalidBotModeError(
            f"Invalid bot_mode: {value!r}. "
            f"Must be one of {sorted(VALID_BOT_MODES)}."
        )
    return value


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

    # LINE channel access token 的 bot/info 驗證狀態。
    # 舊資料可能為 NULL；API 邊界層統一正規化為 "unchecked"。
    credential_status = Column(String(16), nullable=True, default="unchecked")
    credential_last_error = Column(String(255), nullable=True)
    credential_checked_at = Column(DateTime(timezone=True), nullable=True)

    # 預設翻譯目標語言（BCP-47 tag，如 "zh-TW", "en", "ja"）
    default_target_lang = Column(String(16), nullable=False, default="zh-TW")

    # bot 行為模式：translation（預設）/ booking / auto_reply。
    # 雙 default/server_default：server_default 讓 ALTER TABLE 對既有列回填
    # 'translation'，避免 NOT NULL 無預設失敗或殘留 NULL 打到 @validates。
    bot_mode = Column(
        String(16),
        nullable=False,
        default=DEFAULT_BOT_MODE,
        server_default=DEFAULT_BOT_MODE,
    )

    # Rich Menu（圖文選單）套用狀態：LINE 回傳的 richMenuId + 選用的模板/主題。
    # 皆 nullable（未套用為 NULL）；既有 DB 由 _migrate_add_rich_menu_fields() 補欄。
    rich_menu_id = Column(String(64), nullable=True)
    rich_menu_template = Column(String(32), nullable=True)
    rich_menu_theme = Column(String(32), nullable=True)

    # follow 事件（加好友）的自訂歡迎訊息；NULL = 依 bot_mode 用內建預設文案。
    # 既有 DB 由 Alembic rev 0005 補欄。
    welcome_message = Column(Text, nullable=True)

<<<<<<< HEAD
    # 進階選配（A1.1）：租戶自建 LINE Login channel 的 LIFF app id；有值時網頁
    # 預約未來可改以 https://liff.line.me/{liff_id} 開啟（本版僅留欄位）。
    # NULL = 走 token 深連結（預設）。Alembic rev 0007 補欄。
    liff_id = Column(String(64), nullable=True)

=======
>>>>>>> origin/main
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

    @validates("bot_mode")
    def _validate_bot_mode(self, key: str, value: str) -> str:
        """bot_mode 白名單強制——setter/constructor 賦值皆觸發。"""
        return validate_bot_mode(value)

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

"""Email 寄送抽象（B3）— Smtp 實作 + Stub（離線/測試）。

比照 line_client / translator 的可注入模式：
  * ``get_mailer()`` 依 settings 選實作：SAAS_SMTP_HOST 非空 → SmtpMailer，
    否則 StubMailer（寄送內容累積在記憶體，測試斷言用；dev 印 log）。
  * 單機 VPS 建議 SMTP 走外部服務（Gmail SMTP / Mailgun…），自架 MTA 易進垃圾桶。
  * 寄送失敗一律拋 MailerError；呼叫端決定吞或傳播（onboarding 信 best-effort，
    重設密碼信失敗要讓使用者知道）。
"""

from __future__ import annotations

import dataclasses
import logging
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage

from fastapi import Depends
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.db import get_db

_log = logging.getLogger(__name__)


class MailerError(Exception):
    """寄送失敗（連線/認證/協定錯誤的統一包裝）。"""


@dataclasses.dataclass(frozen=True)
class SentMail:
    to: str
    subject: str
    body: str


class Mailer(ABC):
    @abstractmethod
    def send(self, *, to: str, subject: str, body: str) -> None:
        """寄一封純文字信；失敗拋 MailerError。"""


class SmtpMailer(Mailer):
    """stdlib smtplib；STARTTLS（port 587 慣例）。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        from_address: str,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from_address = from_address

    def send(self, *, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=15) as s:
                s.starttls()
                if self._user:
                    s.login(self._user, self._password)
                s.send_message(msg)
        except Exception as exc:  # noqa: BLE001 — 統一包裝
            raise MailerError(f"SMTP send failed: {type(exc).__name__}: {exc}") from exc


class StubMailer(Mailer):
    """離線 stub：寄送累積在 ``sent``（測試斷言）並記 log（dev 可視）。"""

    def __init__(self) -> None:
        self.sent: list[SentMail] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append(SentMail(to=to, subject=subject, body=body))
        _log.info("StubMailer: to=%s subject=%r", to, subject)


class UnconfiguredMailer(Mailer):
    """正式環境未設定 SMTP 時明確失敗，避免 Stub 造成「假裝已寄出」。"""

    def send(self, *, to: str, subject: str, body: str) -> None:
        raise MailerError("Email delivery is not configured")


_stub_singleton = StubMailer()


def get_mailer(db: Session | None = Depends(get_db)) -> Mailer:
    """FastAPI dependency / 直呼皆可；SAAS_SMTP_HOST 非空走真實 SMTP。

    Stub 為模組層單例：同一測試 process 內可跨請求斷言寄送紀錄。
    """
    # FastAPI 依賴呼叫時 db 是 Session；排程/服務直接呼叫 get_mailer() 時預設值
    # 是 Depends 物件，視為無 DB 並使用環境備援。
    actual_db = db if isinstance(db, Session) else None
    from saas_mvp.services.platform_email_config import effective_email_config

    config = effective_email_config(actual_db, settings)
    if config:
        return SmtpMailer(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            from_address=config.from_address,
        )
    if settings.env not in ("dev", "test"):
        return UnconfiguredMailer()
    return _stub_singleton

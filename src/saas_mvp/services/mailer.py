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

from saas_mvp.config import settings

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

    def send(self, *, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = settings.smtp_from or settings.smtp_user
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
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


_stub_singleton = StubMailer()


def get_mailer() -> Mailer:
    """FastAPI dependency / 直呼皆可；SAAS_SMTP_HOST 非空走真實 SMTP。

    Stub 為模組層單例：同一測試 process 內可跨請求斷言寄送紀錄。
    """
    if settings.smtp_host:
        return SmtpMailer()
    return _stub_singleton

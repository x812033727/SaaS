"""簡訊寄送抽象(E3)— Stub 預設 + 三竹 Mitake 真實作(R3-B2)。

完全比照 mailer.py / payment.py 的 stub-ready 模式:
  * ``get_sms_provider()`` 依 settings 選實作:``SAAS_SMS_PROVIDER=mitake``
    且帳密齊 → :class:`MitakeSmsProvider`(三竹企業簡訊 SmSend HTTP API);
    否則(含 mitake 缺憑證,記 warning)→ StubSmsProvider(記憶體累積+log)。
  * 失敗拋 SmsError;呼叫端決定吞或傳播。
  * 唯一掛點:提醒推播失敗且顧客有手機時 fallback(``sms_fallback_enabled``
    預設關,開了也只在 LINE 推播失敗時觸發,不重複打擾)。刻意**不做** outbox
    重試:提醒是時效性 best-effort,排隊補送會變成過時轟炸(email outbox 是
    必達信件語意,不同)。
"""

from __future__ import annotations

import dataclasses
import logging
import re
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

_log = logging.getLogger(__name__)


class SmsError(Exception):
    """簡訊寄送失敗(連線/認證/額度的統一包裝)。"""


@dataclasses.dataclass(frozen=True)
class SentSms:
    to: str
    body: str


class SmsProvider(ABC):
    @abstractmethod
    def send(self, *, to: str, body: str) -> None:
        """寄一則簡訊;失敗拋 SmsError。"""


class StubSmsProvider(SmsProvider):
    """離線 stub:記憶體累積 + log;永遠成功。"""

    def __init__(self) -> None:
        self.sent: list[SentSms] = []

    def send(self, *, to: str, body: str) -> None:
        self.sent.append(SentSms(to=to, body=body))
        _log.info("stub sms to=%s body=%.60s", to, body)


_TW_MOBILE_RE = re.compile(r"^09\d{8}$")


def _normalize_tw_mobile(raw: str) -> str:
    """正規化並驗證台灣手機號(+886→0、去空白連字號);不合法拋 SmsError。"""
    from saas_mvp.services.customer_import import normalize_phone

    phone = normalize_phone(raw) or ""
    if not _TW_MOBILE_RE.match(phone):
        raise SmsError(f"invalid TW mobile number: {raw!r}")
    return phone


def _urllib_post(url: str, body: bytes, headers: dict) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — https only
        return resp.read().decode("utf-8", errors="replace")


class MitakeSmsProvider(SmsProvider):
    """三竹企業簡訊 SmSend HTTP API。

    * ``CharsetURL=UTF8`` 必帶:否則三竹預設 CP950,UTF-8 中文會變亂碼。
    * 回應為多行 ``key=value``(含 ``[N]`` 客戶端編號段):``statuscode`` ∈
      {0,1,2,4}(排程中/已送/已送/已送達)視為成功,其餘(含缺欄、HTTP 錯誤、
      逾時)一律拋 :class:`SmsError`。
    """

    _OK_CODES = {"0", "1", "2", "4"}

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        api_url: str | None = None,
        http_post=None,
    ) -> None:
        from saas_mvp.config import settings

        self._username = username if username is not None else settings.mitake_username
        self._password = password if password is not None else settings.mitake_password
        self._api_url = api_url if api_url is not None else settings.mitake_api_url
        self._http_post = http_post or _urllib_post

    def send(self, *, to: str, body: str) -> None:
        phone = _normalize_tw_mobile(to)
        params = {
            "username": self._username,
            "password": self._password,
            "dstaddr": phone,
            "smbody": body,
        }
        url = f"{self._api_url}?CharsetURL=UTF8"
        encoded = urllib.parse.urlencode(params).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            raw = self._http_post(url, encoded, headers)
        except SmsError:
            raise
        except Exception as exc:  # noqa: BLE001 — 連線/逾時統一包裝
            raise SmsError(f"mitake request failed: {exc}") from exc

        status = self._parse_statuscode(raw)
        if status not in self._OK_CODES:
            raise SmsError(f"mitake rejected: statuscode={status or '(missing)'}")
        _log.info("mitake sms sent to=%s statuscode=%s", phone, status)

    @staticmethod
    def _parse_statuscode(raw: str) -> str:
        for line in raw.splitlines():
            line = line.strip()
            if line.lower().startswith("statuscode="):
                return line.split("=", 1)[1].strip()
        return ""


_stub_singleton = StubSmsProvider()


def get_sms_provider() -> SmsProvider:
    """factory(比照 services/payment.py):mitake+憑證齊 → 真實作;
    mitake 缺憑證 → warning + Stub(stub-ready 慣例);其餘 → Stub 單例。"""
    from saas_mvp.config import settings

    if settings.sms_provider == "mitake":
        if settings.mitake_username and settings.mitake_password:
            return MitakeSmsProvider()
        _log.warning(
            "SAAS_SMS_PROVIDER=mitake but SAAS_MITAKE_USERNAME/PASSWORD missing; "
            "falling back to stub (no real SMS will be sent)"
        )
    return _stub_singleton

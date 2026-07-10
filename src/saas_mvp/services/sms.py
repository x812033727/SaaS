"""簡訊寄送抽象(E3)— 介面先行,Stub 唯一實作;真供應商到位再加實作類。

完全比照 mailer.py 的可注入模式:
  * ``get_sms_provider()`` 依 settings 選實作:目前恆為 StubSmsProvider
    (寄送內容累積在記憶體,測試斷言用;dev 印 log)。真接台灣簡訊商
    (三竹/every8d…)時加 Http 實作 + ``SAAS_SMS_PROVIDER`` 分支。
  * 失敗拋 SmsError;呼叫端決定吞或傳播。
  * 唯一掛點:提醒推播失敗且顧客有手機時 fallback(``sms_fallback_enabled``
    預設關,開了也只在 LINE 推播失敗時觸發,不重複打擾)。
"""

from __future__ import annotations

import dataclasses
import logging
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


_stub_singleton = StubSmsProvider()


def get_sms_provider() -> SmsProvider:
    """factory:尚無真實供應商 → 恆回 Stub 單例。"""
    return _stub_singleton

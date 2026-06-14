"""LINE Reply Client — 抽象基底類別與共用例外。"""

from abc import ABC, abstractmethod


class LineReplyError(Exception):
    """LINE reply API 呼叫失敗（網路錯誤、HTTP 錯誤、回應格式異常）。"""


class LineReplyClient(ABC):
    """LINE Messaging API reply client 介面。

    所有實作（HttpLineReplyClient、FakeLineReplyClient）必須繼承此類別。
    呼叫端只依賴此介面，不知道底層是真實 HTTP 還是 fake。

    access_token 以 per-call 方式傳入（而非 constructor），
    使同一個 client 實例可服務不同 tenant。
    """

    @abstractmethod
    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        """透過 LINE reply API 回覆文字訊息。

        Args:
            reply_token: LINE 事件中的 replyToken（一次性，5 分鐘內有效）。
            text: 要回覆的文字內容。
            access_token: 該 LINE channel 的 channel access token（Bearer）。

        Raises:
            LineReplyError: 網路失敗、HTTP 4xx/5xx、或回應格式不符預期。
        """

    @abstractmethod
    def is_available(self) -> bool:
        """回傳此 client 是否具備發送能力。

        True：已正確設定、可發送。
        False：缺設定或明確停用（如 FakeLineReplyClient 被設為 unavailable 時）。
        """

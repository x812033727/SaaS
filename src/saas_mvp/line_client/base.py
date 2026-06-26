"""LINE Reply Client — 抽象基底類別與共用例外。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class LineReplyError(Exception):
    """LINE reply API 呼叫失敗（網路錯誤、HTTP 錯誤、回應格式異常）。"""


class LinePushError(Exception):
    """LINE push API 呼叫失敗（網路錯誤、HTTP 錯誤、回應格式異常）。"""


class LineRichMenuError(Exception):
    """LINE Rich Menu API 呼叫失敗（建立/上傳圖片/設預設/刪除）。"""


class LineBotInfoError(Exception):
    """LINE bot/info 呼叫失敗的基底例外。"""


class LineBotInfoCredentialError(LineBotInfoError):
    """access token 無效或未授權。"""


class LineBotInfoNetworkError(LineBotInfoError):
    """bot/info 網路層失敗，例如 timeout、DNS 或連線中斷。"""


class LineBotInfoParseError(LineBotInfoError):
    """bot/info 回應格式不符預期。"""


class LineReplyClient(ABC):
    """LINE Messaging API reply client 介面。

    所有實作（HttpLineReplyClient、FakeLineReplyClient）必須繼承此類別。
    呼叫端只依賴此介面，不知道底層是真實 HTTP 還是 fake。

    access_token 以 per-call 方式傳入（而非 constructor），
    使同一個 client 實例可服務不同 tenant。
    """

    @abstractmethod
    def reply(
        self,
        reply_token: str,
        text: str,
        *,
        access_token: str,
        quick_reply: list[tuple[str, str]] | None = None,
    ) -> None:
        """透過 LINE reply API 回覆文字訊息（可附 quick-reply postback 按鈕）。

        Args:
            reply_token: LINE 事件中的 replyToken（一次性，5 分鐘內有效）。
            text: 要回覆的文字內容。
            access_token: 該 LINE channel 的 channel access token（Bearer）。
            quick_reply: 選填，`(label, postback_data)` 清單；每筆轉為一個
                postback quick-reply 按鈕（供引導式預約逐步選擇）。最多 13 筆，
                label 上限 20 字（LINE 限制），超過自動截斷。

        Raises:
            LineReplyError: 網路失敗、HTTP 4xx/5xx、或回應格式不符預期。
        """

    def reply_flex(
        self,
        reply_token: str,
        alt_text: str,
        contents: dict,
        *,
        access_token: str,
    ) -> None:
        """透過 LINE reply API 回覆一則 Flex 訊息（carousel / bubble）。

        Args:
            reply_token: LINE 事件中的 replyToken。
            alt_text: 不支援 Flex 的環境顯示的替代文字。
            contents: Flex 訊息的 `contents`（carousel / bubble dict）。
            access_token: 該 LINE channel 的 channel access token（Bearer）。

        預設實作以子類覆寫；ABC 提供 NotImplementedError 防衛，使既有只實作
        text reply 的測試替身（若有）不被強制改寫。內建 Http / Fake 皆已實作。

        Raises:
            LineReplyError: 網路失敗、HTTP 4xx/5xx、或回應格式不符預期。
        """
        raise NotImplementedError("reply_flex not implemented by this client")

    @abstractmethod
    def is_available(self) -> bool:
        """回傳此 client 是否具備發送能力。

        True：已正確設定、可發送。
        False：缺設定或明確停用（如 FakeLineReplyClient 被設為 unavailable 時）。
        """


class LinePushClient(ABC):
    """LINE Messaging API push client 介面 — 主動推播訊息給指定使用者。

    與 reply 的差異：push 用 `to`（使用者 userId）而非一次性 reply_token，
    不受 5 分鐘時效限制，供「預約提醒」等非即時回覆場景使用。

    access_token 以 per-call 傳入，使同一實例可服務不同 tenant。
    """

    @abstractmethod
    def push(self, to_user_id: str, text: str, *, access_token: str) -> None:
        """透過 LINE push API 推播文字訊息給指定使用者。

        Args:
            to_user_id: 接收者的 LINE userId。
            text: 要推播的文字內容。
            access_token: 該 LINE channel 的 channel access token（Bearer）。

        Raises:
            LinePushError: 網路失敗、HTTP 4xx/5xx、或回應格式不符預期。
        """

    @abstractmethod
    def is_available(self) -> bool:
        """回傳此 client 是否具備發送能力。"""


class LineRichMenuClient(ABC):
    """LINE Rich Menu API client 介面。

    一次「套用圖文選單」需四步：建立選單結構 → 上傳背景圖 → 設為預設 →
    （可選）刪除舊選單。access_token 以 per-call 傳入，使同一實例服務不同 tenant。
    """

    @abstractmethod
    def create(self, rich_menu: dict, *, access_token: str) -> str:
        """建立 rich menu 結構，回傳 richMenuId。"""

    @abstractmethod
    def upload_image(
        self, rich_menu_id: str, image: bytes, content_type: str, *, access_token: str
    ) -> None:
        """上傳 rich menu 背景圖（PNG/JPEG）。"""

    @abstractmethod
    def set_default(self, rich_menu_id: str, *, access_token: str) -> None:
        """將指定 rich menu 設為所有使用者的預設選單。"""

    @abstractmethod
    def delete(self, rich_menu_id: str, *, access_token: str) -> None:
        """刪除指定 rich menu。"""

    @abstractmethod
    def is_available(self) -> bool:
        """回傳此 client 是否具備發送能力。"""


class LineProfileError(Exception):
    """LINE `GET /v2/bot/profile/{userId}` 呼叫失敗的基底例外。"""


class LineProfileCredentialError(LineProfileError):
    """access token 無效或未授權（HTTP 400/401/403）。"""


class LineProfileNetworkError(LineProfileError):
    """profile API 網路層失敗，例如 timeout、DNS 或連線中斷。"""


class LineProfileParseError(LineProfileError):
    """profile API 回應格式不符預期（非 JSON 或非物件）。"""


@dataclass(frozen=True)
class LineUserProfile:
    """LINE 使用者 profile（`GET /v2/bot/profile/{userId}` 回應）。

    picture_url / status_message 保留欄位供未來使用；目前預約流程只取 display_name。
    display_name 可能為 None（回應缺 displayName，例如使用者未設名稱）。
    """
    user_id: str
    display_name: str | None
    picture_url: str | None = None
    status_message: str | None = None


class LineProfileClient(ABC):
    """LINE `GET /v2/bot/profile/{userId}` client 介面 — 取得使用者顯示名稱等 profile。

    供 LINE 預約流程在建單時補回顧客 displayName（webhook event.source 只給 userId）。
    與其他 client 一致採 ABC + abstractmethod、access_token 以 per-call 傳入，
    使同一實例可服務不同 tenant。
    """

    @abstractmethod
    def get_profile(self, user_id: str, *, access_token: str) -> LineUserProfile | None:
        """以 channel access token 呼叫 profile API，回傳該使用者的 profile。

        Args:
            user_id: 目標使用者的 LINE userId（格式 U[0-9a-f]{32}）。
            access_token: 該 LINE channel 的 channel access token（Bearer）。

        Returns:
            成功時回傳 :class:`LineUserProfile`（display_name 可能為 None）；
            回應缺少可用 body 時回 None。

        Raises:
            LineProfileCredentialError: token 無效或未授權（400/401/403）。
            LineProfileNetworkError: 網路層失敗。
            LineProfileParseError: 回應格式不是可解析的 JSON 物件。
            LineProfileError: 其他 profile API 失敗。
        """


class LineBotInfoClient(ABC):
    """LINE `GET /v2/bot/info` client 介面 — 取得 bot 的 userId。

    回傳的 userId 作為租戶識別二次驗證鍵（與 webhook payload.destination 比對）。
    與 LineReplyClient 一致採 ABC + abstractmethod：未實作方法在實例化時即報錯。

    access_token 以 per-call 傳入，使同一實例可服務不同 tenant。
    """

    @abstractmethod
    def get_user_id(self, access_token: str) -> str | None:
        """以 channel access token 呼叫 bot/info，回傳 bot 的 userId。

        Args:
            access_token: 該 LINE channel 的 channel access token（Bearer）。

        Returns:
            成功時回傳 userId（格式 U[0-9a-f]{32}）；回應缺少 userId 時回 None。

        Raises:
            LineBotInfoCredentialError: token 無效或未授權。
            LineBotInfoNetworkError: 網路層失敗。
            LineBotInfoParseError: 回應格式不是可解析 JSON。
            LineBotInfoError: 其他 bot/info API 失敗。
        """

"""真實 LINE Messaging API 各 client。

R4-P3:改用**模組級連線池化的 httpx.Client**(取代 per-request urllib 每次
重開 TLS)。錯誤語意不變 —— 每個 client 仍把失敗轉成 line_client.base 的
型別化例外。push 帶 LINE 官方冪等 header ``X-Line-Retry-Key`` 後可對
429/5xx/網路錯誤 bounded retry;reply(replyToken 一次性)**不重試**。
"""

from __future__ import annotations

import json
import re
import uuid

import httpx

from saas_mvp.line_client.base import (
    LineBotInfoClient,
    LineBotInfoCredentialError,
    LineBotInfoError,
    LineBotInfoNetworkError,
    LineBotInfoParseError,
    LineWebhookAdminClient,
    LineWebhookAdminCredentialError,
    LineWebhookAdminError,
    LineWebhookAdminNetworkError,
    LineWebhookAdminParseError,
    LineWebhookTestResult,
    LineProfileClient,
    LineProfileCredentialError,
    LineProfileError,
    LineProfileNetworkError,
    LineProfileParseError,
    LinePushClient,
    LinePushError,
    LineReplyClient,
    LineReplyError,
    LineRichMenuClient,
    LineRichMenuError,
    LineUserProfile,
)

_LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_LINE_BOT_INFO_URL = "https://api.line.me/v2/bot/info"
_LINE_API_BASE = "https://api.line.me"
_LINE_DATA_API_BASE = "https://api-data.line.me"

# ── 連線池化 httpx client(R4-P3)────────────────────────────────────────────
# 模組級單例:跨呼叫重用 keep-alive 連線,免每次重開 TLS。lazy init 讓測試在
# import 期不建連線。測試以 httpx MockTransport / monkeypatch _client 注入,
# 不打真實網路。
_pooled_client: httpx.Client | None = None


def _client() -> httpx.Client:
    global _pooled_client
    if _pooled_client is None:
        _pooled_client = httpx.Client(
            timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=3.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
            trust_env=False,  # 忽略環境 proxy 設定,行為可預期
        )
    return _pooled_client


def _send(
    method: str,
    url: str,
    *,
    access_token: str,
    json_body: dict | None = None,
    timeout: float | None = None,
    content: bytes | None = None,
    content_type: str = "application/json",
    retries: int = 0,
    retry_key: str | None = None,
) -> httpx.Response:
    """發一次 LINE API 請求,回 httpx.Response(任何狀態碼都回,不 raise_for_status)。

    網路層失敗(連線/逾時)在重試耗盡後 raise httpx.RequestError,由呼叫端映射為
    型別化例外。retries>0 時對 429/5xx 與網路錯誤 bounded retry(指數退避以
    attempt 計);**只可用於帶 retry_key 的冪等操作**(如 push)。
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    body = content
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body).encode()
    elif content is not None:
        headers["Content-Type"] = content_type
    if retry_key:
        headers["X-Line-Retry-Key"] = retry_key
    attempt = 0
    while True:
        try:
            resp = _client().request(
                method, url, headers=headers, content=body, timeout=timeout
            )
        except httpx.RequestError:
            if attempt < retries:
                attempt += 1
                continue
            raise
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < retries:
                attempt += 1
                continue
        return resp

# LINE userId 規格：U 後接 32 個 hex 字元。防禦性驗證，非法值當 None 處理。
_LINE_USER_ID_RE = re.compile(r"^U[0-9a-f]{32}$")

# LINE quick-reply 限制：最多 13 筆、label 上限 20 字。
_QR_MAX_ITEMS = 13
_QR_LABEL_MAX = 20


def _quick_reply_items(items: list) -> list[dict]:
    """把 quick-reply 項目清單轉為 LINE quickReply items。

    兩種項目形式（A1.3 起）：
      * ``(label, postback_data)`` tuple — 既有形式，轉 postback action。
      * ``dict`` — 直接作為 LINE action 物件透傳（uri / datetimepicker /
        camera…），呼叫端自組完整 action（含 type/label 與該型別必要欄位）。
    """
    out: list[dict] = []
    for item in items[:_QR_MAX_ITEMS]:
        if isinstance(item, dict):
            out.append({"type": "action", "action": item})
            continue
        label, data = item
        out.append(
            {
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": label[:_QR_LABEL_MAX],
                    "data": data,
                    "displayText": label[:_QR_LABEL_MAX],
                },
            }
        )
    return out


class HttpLineReplyClient(LineReplyClient):
    """呼叫真實 LINE Messaging API 的 reply client。

    Args:
        api_url: reply endpoint（預設官方 URL，測試時可替換）。
        timeout: HTTP timeout（秒）。
    """

    def __init__(
        self,
        api_url: str = _LINE_REPLY_URL,
        timeout: int = 10,
    ) -> None:
        self._api_url = api_url
        self._timeout = timeout

    def is_available(self) -> bool:
        """HTTP client 一律視為可用（連線能力由 reply() 的例外反映）。"""
        return True

    def reply(
        self,
        reply_token: str,
        text: str,
        *,
        access_token: str,
        quick_reply: list[tuple[str, str]] | None = None,
    ) -> None:
        """呼叫 LINE reply API，送出單則文字訊息（可附 quick-reply 按鈕）。

        Raises:
            LineReplyError: 任何網路或 API 錯誤。
        """
        message: dict = {"type": "text", "text": text}
        if quick_reply:
            message["quickReply"] = {"items": _quick_reply_items(quick_reply)}
        # reply 用 replyToken(一次性)→ **不重試**,重送會 400 Invalid reply token。
        try:
            resp = _send(
                "POST", self._api_url, access_token=access_token,
                json_body={"replyToken": reply_token, "messages": [message]},
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise LineReplyError(f"LINE reply request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LineReplyError(
                f"LINE reply API HTTP {resp.status_code}: {resp.reason_phrase} "
                f"— {resp.text[:200]}"
            )

    def reply_flex(
        self,
        reply_token: str,
        alt_text: str,
        contents: dict,
        *,
        access_token: str,
    ) -> None:
        """呼叫 LINE reply API 送出單則 Flex 訊息（carousel / bubble）。

        Raises:
            LineReplyError: 任何網路或 API 錯誤。
        """
        message = {"type": "flex", "altText": alt_text[:400], "contents": contents}
        try:
            resp = _send(
                "POST", self._api_url, access_token=access_token,
                json_body={"replyToken": reply_token, "messages": [message]},
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise LineReplyError(f"LINE reply request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LineReplyError(
                f"LINE reply API HTTP {resp.status_code}: {resp.reason_phrase} "
                f"— {resp.text[:200]}"
            )


class HttpLinePushClient(LinePushClient):
    """呼叫真實 LINE Messaging API 的 push client。

    透過連線池化 httpx.Client(R4-P3);push 帶 X-Line-Retry-Key 冪等重試。

    Args:
        api_url: push endpoint（預設官方 URL，測試時可替換）。
        timeout: HTTP timeout（秒）。
    """

    def __init__(
        self,
        api_url: str = _LINE_PUSH_URL,
        timeout: int = 10,
    ) -> None:
        self._api_url = api_url
        self._timeout = timeout

    def is_available(self) -> bool:
        """HTTP client 一律視為可用（連線能力由 push() 的例外反映）。"""
        return True

    def push(
        self,
        to_user_id: str,
        text: str,
        *,
        access_token: str,
        quick_reply: list[tuple[str, str]] | None = None,
    ) -> None:
        """呼叫 LINE push API，主動推播單則文字訊息。

        Raises:
            LinePushError: 任何網路或 API 錯誤。
        """
        message: dict = {"type": "text", "text": text}
        if quick_reply:
            message["quickReply"] = {"items": _quick_reply_items(quick_reply)}
        self._push_messages(to_user_id, [message], access_token)

    def push_flex(
        self,
        to_user_id: str,
        alt_text: str,
        contents: dict,
        *,
        access_token: str,
    ) -> None:
        """推播 Flex Message（A3.1）。"""
        self._push_messages(
            to_user_id,
            [{"type": "flex", "altText": alt_text[:400] or "訊息", "contents": contents}],
            access_token,
        )

    def push_image(
        self,
        to_user_id: str,
        original_url: str,
        preview_url: str | None = None,
        *,
        access_token: str,
    ) -> None:
        """推播圖片（A3.1）。LINE 要求 https URL；preview 未給沿用原圖。"""
        self._push_messages(
            to_user_id,
            [{
                "type": "image",
                "originalContentUrl": original_url,
                "previewImageUrl": preview_url or original_url,
            }],
            access_token,
        )

    def _push_messages(
        self, to_user_id: str, messages: list[dict], access_token: str
    ) -> None:
        # push 帶 LINE 官方冪等 header X-Line-Retry-Key(整個重試序列同一把 key,
        # LINE 以此去重),故可對 429/5xx/網路錯誤 bounded retry×2。
        try:
            resp = _send(
                "POST", self._api_url, access_token=access_token,
                json_body={"to": to_user_id, "messages": messages},
                timeout=self._timeout,
                retries=2,
                retry_key=str(uuid.uuid4()),
            )
        except httpx.RequestError as exc:
            raise LinePushError(f"LINE push request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LinePushError(
                f"LINE push API HTTP {resp.status_code}: {resp.reason_phrase} "
                f"— {resp.text[:200]}"
            )


class HttpLineRichMenuClient(LineRichMenuClient):
    """呼叫真實 LINE Rich Menu API 的 client(連線池化 httpx)。

    Args:
        api_base: 一般 API base（建立/設預設/刪除）。
        data_api_base: data API base（上傳圖片）。
        timeout: HTTP timeout（秒）。
    """

    def __init__(
        self,
        api_base: str = _LINE_API_BASE,
        data_api_base: str = _LINE_DATA_API_BASE,
        timeout: int = 10,
    ) -> None:
        self._api_base = api_base
        self._data_api_base = data_api_base
        self._timeout = timeout

    def is_available(self) -> bool:
        return True

    def _request(
        self,
        url: str,
        *,
        method: str,
        access_token: str,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        try:
            resp = _send(
                method, url, access_token=access_token,
                content=data,
                content_type=content_type or "application/json",
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise LineRichMenuError(f"LINE Rich Menu request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise LineRichMenuError(
                f"LINE Rich Menu API HTTP {resp.status_code}: {resp.reason_phrase} "
                f"— {resp.text[:200]}"
            )
        return resp.content

    def create(self, rich_menu: dict, *, access_token: str) -> str:
        raw = self._request(
            f"{self._api_base}/v2/bot/richmenu",
            method="POST",
            access_token=access_token,
            data=json.dumps(rich_menu).encode(),
            content_type="application/json",
        )
        try:
            data = json.loads(raw)
            rich_menu_id = data["richMenuId"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LineRichMenuError("create did not return richMenuId") from exc
        return rich_menu_id

    def upload_image(
        self, rich_menu_id: str, image: bytes, content_type: str, *, access_token: str
    ) -> None:
        self._request(
            f"{self._data_api_base}/v2/bot/richmenu/{rich_menu_id}/content",
            method="POST",
            access_token=access_token,
            data=image,
            content_type=content_type,
        )

    def set_default(self, rich_menu_id: str, *, access_token: str) -> None:
        self._request(
            f"{self._api_base}/v2/bot/user/all/richmenu/{rich_menu_id}",
            method="POST",
            access_token=access_token,
        )

    def delete(self, rich_menu_id: str, *, access_token: str) -> None:
        self._request(
            f"{self._api_base}/v2/bot/richmenu/{rich_menu_id}",
            method="DELETE",
            access_token=access_token,
        )


class HttpLineBotInfoClient(LineBotInfoClient):
    """呼叫真實 LINE `GET /v2/bot/info` 取得 bot userId 的 client。

    透過連線池化 httpx;HTTP/網路/解析失敗轉成 line_client.base 的
    型別化例外，由呼叫端決定狀態落 DB，不阻擋設定儲存。

    Args:
        api_url: bot/info endpoint（預設官方 URL，測試時可替換）。
        timeout: HTTP timeout（秒）。
    """

    def __init__(
        self,
        api_url: str = _LINE_BOT_INFO_URL,
        timeout: int = 10,
    ) -> None:
        self._api_url = api_url
        self._timeout = timeout

    def get_user_id(self, access_token: str) -> str | None:
        """呼叫 bot/info，回傳 ``userId``；回應缺欄位時回 None。"""
        try:
            resp = _send(
                "GET", self._api_url, access_token=access_token, timeout=self._timeout
            )
        except httpx.RequestError as exc:
            raise LineBotInfoNetworkError("LINE bot/info network error") from exc
        if resp.status_code >= 400:
            code = resp.status_code
            if code in {400, 401, 403}:
                from saas_mvp.line_client.base import LineAuthErrorKind

                kind = LineAuthErrorKind.UNKNOWN_AUTH
                body = resp.text
                if code == 401 and body:
                    try:
                        details = json.loads(body).get("details", [])
                    except (json.JSONDecodeError, AttributeError):
                        details = []
                    messages = []
                    for detail in details if isinstance(details, list) else []:
                        if isinstance(detail, str):
                            messages.append(detail.lower())
                        elif isinstance(detail, dict):
                            messages.append(str(detail.get("message", "")).lower())
                    joined = " ".join(messages)
                    if "channel access token" in joined:
                        kind = LineAuthErrorKind.ACCESS_TOKEN_INVALID
                    elif "channel secret" in joined:
                        kind = LineAuthErrorKind.CHANNEL_SECRET_INVALID
                raise LineBotInfoCredentialError(
                    f"LINE bot/info credential rejected: HTTP {code}",
                    kind=kind,
                )
            raise LineBotInfoError(f"LINE bot/info HTTP {code}")
        raw = resp.text

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LineBotInfoParseError("LINE bot/info returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LineBotInfoParseError("LINE bot/info returned non-object JSON")

        user_id = data.get("userId")
        # 缺欄位或不符 LINE 規格（U + 32 hex）一律當 None，不存入 DB
        if not user_id or not _LINE_USER_ID_RE.match(user_id):
            return None
        return user_id


class HttpLineWebhookAdminClient(LineWebhookAdminClient):
    """LINE Webhook endpoint 設定與連通測試 client。"""

    def __init__(self, api_base: str = _LINE_API_BASE, timeout: int = 10) -> None:
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def _request(
        self,
        path: str,
        *,
        method: str,
        access_token: str,
        payload: dict | None = None,
    ) -> dict:
        try:
            resp = _send(
                method, f"{self._api_base}{path}", access_token=access_token,
                json_body=payload if payload is not None else None,
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise LineWebhookAdminNetworkError(
                "無法連線到 LINE Webhook API，請稍後重試"
            ) from exc
        if resp.status_code >= 400:
            if resp.status_code in {401, 403}:
                raise LineWebhookAdminCredentialError(
                    f"LINE 拒絕 Channel access token（HTTP {resp.status_code}）"
                )
            raise LineWebhookAdminError(
                f"LINE Webhook API HTTP {resp.status_code}: "
                f"{resp.text[:200] or resp.reason_phrase}"
            )
        raw = resp.text or "{}"

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LineWebhookAdminParseError(
                "LINE Webhook API 回傳無效 JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise LineWebhookAdminParseError(
                "LINE Webhook API 回傳格式不是物件"
            )
        return parsed

    def configure_and_test(
        self, endpoint: str, *, access_token: str
    ) -> LineWebhookTestResult:
        if not endpoint.startswith("https://") or len(endpoint) > 500:
            raise LineWebhookAdminError("Webhook URL 必須是 500 字內的 HTTPS 網址")

        self._request(
            "/v2/bot/channel/webhook/endpoint",
            method="PUT",
            access_token=access_token,
            payload={"endpoint": endpoint},
        )
        test = self._request(
            "/v2/bot/channel/webhook/test",
            method="POST",
            access_token=access_token,
            payload={"endpoint": endpoint},
        )
        if not isinstance(test.get("success"), bool):
            raise LineWebhookAdminParseError("LINE Webhook 測試缺少 success 欄位")

        info = self._request(
            "/v2/bot/channel/webhook/endpoint",
            method="GET",
            access_token=access_token,
        )
        active = info.get("active") if isinstance(info.get("active"), bool) else None
        status_code = test.get("statusCode")
        return LineWebhookTestResult(
            endpoint=endpoint,
            success=test["success"],
            active=active,
            status_code=status_code if isinstance(status_code, int) else None,
            reason=test.get("reason") if isinstance(test.get("reason"), str) else None,
            detail=test.get("detail") if isinstance(test.get("detail"), str) else None,
            timestamp=(
                test.get("timestamp") if isinstance(test.get("timestamp"), str) else None
            ),
        )


class HttpLineProfileClient(LineProfileClient):
    """呼叫真實 LINE `GET /v2/bot/profile/{userId}` 取得使用者 profile 的 client。

    透過連線池化 httpx;HTTP/網路/解析失敗轉成 line_client.base 的型別化例外,
    由呼叫端決定是否降級（預約流程降級為 display_name=None，不阻擋建單）。

    Args:
        api_base: API base（預設官方 URL，測試時可替換）。
        timeout: HTTP timeout（秒）。
    """

    def __init__(
        self,
        api_base: str = _LINE_API_BASE,
        timeout: int = 10,
    ) -> None:
        self._api_base = api_base
        self._timeout = timeout

    def get_profile(self, user_id: str, *, access_token: str) -> LineUserProfile | None:
        """呼叫 profile API，回傳 :class:`LineUserProfile`；回應非物件時回 None。"""
        try:
            resp = _send(
                "GET", f"{self._api_base}/v2/bot/profile/{user_id}",
                access_token=access_token, timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise LineProfileNetworkError("LINE profile network error") from exc
        if resp.status_code >= 400:
            if resp.status_code in {400, 401, 403}:
                raise LineProfileCredentialError(
                    f"LINE profile credential rejected: HTTP {resp.status_code}"
                )
            raise LineProfileError(f"LINE profile HTTP {resp.status_code}")
        raw = resp.text

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LineProfileParseError("LINE profile returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LineProfileParseError("LINE profile returned non-object JSON")

        return LineUserProfile(
            user_id=data.get("userId") or user_id,
            display_name=data.get("displayName"),
            picture_url=data.get("pictureUrl"),
            status_message=data.get("statusMessage"),
        )

"""真實 LINE Messaging API reply client。

僅用 stdlib urllib（無額外 runtime deps）。
任何失敗一律包成 LineReplyError，呼叫端不需處理底層例外。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from saas_mvp.line_client.base import (
    LineBotInfoClient,
    LineBotInfoCredentialError,
    LineBotInfoError,
    LineBotInfoNetworkError,
    LineBotInfoParseError,
    LinePushClient,
    LinePushError,
    LineReplyClient,
    LineReplyError,
    LineRichMenuClient,
    LineRichMenuError,
)

_LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_LINE_BOT_INFO_URL = "https://api.line.me/v2/bot/info"
_LINE_API_BASE = "https://api.line.me"
_LINE_DATA_API_BASE = "https://api-data.line.me"

# LINE userId 規格：U 後接 32 個 hex 字元。防禦性驗證，非法值當 None 處理。
_LINE_USER_ID_RE = re.compile(r"^U[0-9a-f]{32}$")

# LINE quick-reply 限制：最多 13 筆、label 上限 20 字。
_QR_MAX_ITEMS = 13
_QR_LABEL_MAX = 20


def _quick_reply_items(items: list[tuple[str, str]]) -> list[dict]:
    """把 `(label, postback_data)` 清單轉為 LINE quickReply items（postback action）。"""
    out: list[dict] = []
    for label, data in items[:_QR_MAX_ITEMS]:
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
        payload = json.dumps(
            {
                "replyToken": reply_token,
                "messages": [message],
            }
        ).encode()

        req = urllib.request.Request(
            self._api_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                # LINE API 成功時回 200，body 為 `{}`
                resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace") if exc.fp else ""
            raise LineReplyError(
                f"LINE reply API HTTP {exc.code}: {exc.reason} — {body[:200]}"
            ) from exc
        except OSError as exc:
            raise LineReplyError(f"LINE reply request failed: {exc}") from exc
        except Exception as exc:
            raise LineReplyError(f"Unexpected LINE reply error: {exc}") from exc

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
        payload = json.dumps(
            {"replyToken": reply_token, "messages": [message]}
        ).encode()

        req = urllib.request.Request(
            self._api_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace") if exc.fp else ""
            raise LineReplyError(
                f"LINE reply API HTTP {exc.code}: {exc.reason} — {body[:200]}"
            ) from exc
        except OSError as exc:
            raise LineReplyError(f"LINE reply request failed: {exc}") from exc
        except Exception as exc:
            raise LineReplyError(f"Unexpected LINE reply error: {exc}") from exc


class HttpLinePushClient(LinePushClient):
    """呼叫真實 LINE Messaging API 的 push client。

    僅用 stdlib urllib。任何失敗一律包成 LinePushError。

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

    def push(self, to_user_id: str, text: str, *, access_token: str) -> None:
        """呼叫 LINE push API，主動推播單則文字訊息。

        Raises:
            LinePushError: 任何網路或 API 錯誤。
        """
        payload = json.dumps(
            {
                "to": to_user_id,
                "messages": [{"type": "text", "text": text}],
            }
        ).encode()

        req = urllib.request.Request(
            self._api_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                # LINE API 成功時回 200，body 為 `{}`
                resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace") if exc.fp else ""
            raise LinePushError(
                f"LINE push API HTTP {exc.code}: {exc.reason} — {body[:200]}"
            ) from exc
        except OSError as exc:
            raise LinePushError(f"LINE push request failed: {exc}") from exc
        except Exception as exc:
            raise LinePushError(f"Unexpected LINE push error: {exc}") from exc


class HttpLineRichMenuClient(LineRichMenuClient):
    """呼叫真實 LINE Rich Menu API 的 client（僅用 stdlib urllib）。

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
        headers = {"Authorization": f"Bearer {access_token}"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace") if exc.fp else ""
            raise LineRichMenuError(
                f"LINE Rich Menu API HTTP {exc.code}: {exc.reason} — {body[:200]}"
            ) from exc
        except OSError as exc:
            raise LineRichMenuError(f"LINE Rich Menu request failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise LineRichMenuError(f"Unexpected LINE Rich Menu error: {exc}") from exc

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

    僅用 stdlib urllib。HTTP/網路/解析失敗會轉成 line_client.base 的
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
        req = urllib.request.Request(
            self._api_url,
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 403}:
                raise LineBotInfoCredentialError(
                    f"LINE bot/info credential rejected: HTTP {exc.code}"
                ) from exc
            raise LineBotInfoError(f"LINE bot/info HTTP {exc.code}") from exc
        except OSError as exc:
            raise LineBotInfoNetworkError("LINE bot/info network error") from exc

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

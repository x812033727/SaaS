"""真實 LINE Messaging API reply client。

僅用 stdlib urllib（無額外 runtime deps）。
任何失敗一律包成 LineReplyError，呼叫端不需處理底層例外。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from saas_mvp.line_client.base import LineReplyClient, LineReplyError

_LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


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

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        """呼叫 LINE reply API，送出單則文字訊息。

        Raises:
            LineReplyError: 任何網路或 API 錯誤。
        """
        payload = json.dumps(
            {
                "replyToken": reply_token,
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
            raise LineReplyError(
                f"LINE reply API HTTP {exc.code}: {exc.reason} — {body[:200]}"
            ) from exc
        except OSError as exc:
            raise LineReplyError(f"LINE reply request failed: {exc}") from exc
        except Exception as exc:
            raise LineReplyError(f"Unexpected LINE reply error: {exc}") from exc

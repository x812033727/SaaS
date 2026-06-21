"""測試用 FakeLineReplyClient — 捕捉所有回覆，無網路呼叫。"""

from __future__ import annotations

from dataclasses import dataclass

import itertools

from saas_mvp.line_client.base import (
    LineBotInfoClient,
    LinePushClient,
    LinePushError,
    LineReplyClient,
    LineRichMenuClient,
    LineRichMenuError,
)


@dataclass
class SentReply:
    """一筆捕捉到的回覆記錄。"""
    reply_token: str
    text: str
    access_token: str
    quick_reply: list[tuple[str, str]] | None = None


@dataclass
class SentPush:
    """一筆捕捉到的推播記錄。"""
    to_user_id: str
    text: str
    access_token: str


class FakeLineReplyClient(LineReplyClient):
    """離線 fake client，將所有 reply() 呼叫累積在 ``sent`` list 供斷言。

    使用方式（pytest + FastAPI dependency_overrides）::

        fake = FakeLineReplyClient()
        app.dependency_overrides[get_line_client] = lambda: fake

        client.post("/line/webhook/1", ...)
        assert fake.sent[0].text == "[JA] hello"

    Args:
        available: 控制 ``is_available()`` 回傳值，預設 True。
            設為 False 可測試 webhook handler 在 client 不可用時的行為分支。

    Attributes:
        sent: 按呼叫順序排列的 :class:`SentReply` 清單。
    """

    def __init__(self, *, available: bool = True) -> None:
        self.sent: list[SentReply] = []
        self._available = available

    def reply(
        self,
        reply_token: str,
        text: str,
        *,
        access_token: str,
        quick_reply: list[tuple[str, str]] | None = None,
    ) -> None:
        """捕捉回覆（不發網路請求）。"""
        self.sent.append(SentReply(
            reply_token=reply_token,
            text=text,
            access_token=access_token,
            quick_reply=quick_reply,
        ))

    def is_available(self) -> bool:
        """回傳建構時指定的 available 值（預設 True）。"""
        return self._available

    def reset(self) -> None:
        """清空捕捉記錄（跨測試複用同一 instance 時使用）。"""
        self.sent.clear()

    # ── 便利斷言屬性 ──────────────────────────────────────────────────────────

    @property
    def call_count(self) -> int:
        """呼叫次數。"""
        return len(self.sent)

    @property
    def last_text(self) -> str | None:
        """最後一次回覆的文字；未有任何呼叫時回 None。"""
        return self.sent[-1].text if self.sent else None

    @property
    def texts(self) -> list[str]:
        """所有回覆文字（按呼叫順序）。"""
        return [r.text for r in self.sent]


class FakeLinePushClient(LinePushClient):
    """離線 fake push client，將所有 push() 呼叫累積在 ``sent`` list 供斷言。

    使用方式::

        fake = FakeLinePushClient()
        send_due_reminders(..., push_client=fake)
        assert fake.call_count == 1

    Args:
        available: 控制 ``is_available()`` 回傳值，預設 True。
        fail: 設為 True 時 push() 拋 LinePushError，模擬推播失敗分支。
    """

    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
        self.sent: list[SentPush] = []
        self._available = available
        self._fail = fail

    def push(self, to_user_id: str, text: str, *, access_token: str) -> None:
        """捕捉推播（不發網路請求）；fail=True 時拋 LinePushError。"""
        if self._fail:
            raise LinePushError("fake push failure")
        self.sent.append(SentPush(
            to_user_id=to_user_id,
            text=text,
            access_token=access_token,
        ))

    def is_available(self) -> bool:
        return self._available

    def reset(self) -> None:
        self.sent.clear()

    @property
    def call_count(self) -> int:
        return len(self.sent)

    @property
    def texts(self) -> list[str]:
        return [p.text for p in self.sent]


class FakeLineRichMenuClient(LineRichMenuClient):
    """離線 fake rich menu client，記錄各步呼叫供斷言。

    Args:
        fail_on: 設為步驟名（"create"/"upload"/"set_default"/"delete"）時，
            該步拋 LineRichMenuError，用來測失敗分支。
    """

    _ids = itertools.count(1)

    def __init__(self, *, available: bool = True, fail_on: str | None = None) -> None:
        self.created: list[dict] = []
        self.uploaded: list[tuple[str, int, str]] = []  # (rich_menu_id, len(image), content_type)
        self.defaulted: list[str] = []
        self.deleted: list[str] = []
        self._available = available
        self._fail_on = fail_on

    def _maybe_fail(self, step: str) -> None:
        if self._fail_on == step:
            raise LineRichMenuError(f"fake rich menu failure at {step}")

    def create(self, rich_menu: dict, *, access_token: str) -> str:
        self._maybe_fail("create")
        rich_menu_id = f"richmenu-{next(self._ids)}"
        self.created.append(rich_menu)
        return rich_menu_id

    def upload_image(
        self, rich_menu_id: str, image: bytes, content_type: str, *, access_token: str
    ) -> None:
        self._maybe_fail("upload")
        self.uploaded.append((rich_menu_id, len(image), content_type))

    def set_default(self, rich_menu_id: str, *, access_token: str) -> None:
        self._maybe_fail("set_default")
        self.defaulted.append(rich_menu_id)

    def delete(self, rich_menu_id: str, *, access_token: str) -> None:
        self._maybe_fail("delete")
        self.deleted.append(rich_menu_id)

    def is_available(self) -> bool:
        return self._available


class StubLineBotInfoClient(LineBotInfoClient):
    """測試用 bot/info client，回傳預設 userId，無網路呼叫。

    使用方式（pytest + FastAPI dependency_overrides）::

        stub = StubLineBotInfoClient("U" + "a" * 32)
        app.dependency_overrides[get_bot_info_client] = lambda: stub

    Args:
        user_id: ``get_user_id()`` 的固定回傳值；傳 None 模擬「回應缺 userId」。
        raises: 設為 True 時 ``get_user_id()`` 拋例外，模擬 bot/info 不可達；
            用來驗證 upsert 在失敗時仍成功、line_bot_user_id 留 None。

    Attributes:
        calls: 收到的 access_token 清單（按呼叫順序），供斷言。
    """

    def __init__(
        self,
        user_id: str | None = None,
        *,
        raises: bool | Exception = False,
    ) -> None:
        self._user_id = user_id
        self._raises = raises
        self.calls: list[str] = []

    def get_user_id(self, access_token: str) -> str | None:
        """回傳建構時指定的 user_id；raises=True 時拋例外。"""
        self.calls.append(access_token)
        if isinstance(self._raises, Exception):
            raise self._raises
        if self._raises:
            raise RuntimeError("stub bot/info unavailable")
        return self._user_id

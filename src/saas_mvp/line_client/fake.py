"""測試用 FakeLineReplyClient — 捕捉所有回覆，無網路呼叫。"""

from __future__ import annotations

from dataclasses import dataclass

from saas_mvp.line_client.base import LineReplyClient


@dataclass
class SentReply:
    """一筆捕捉到的回覆記錄。"""
    reply_token: str
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

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        """捕捉回覆（不發網路請求）。"""
        self.sent.append(SentReply(
            reply_token=reply_token,
            text=text,
            access_token=access_token,
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

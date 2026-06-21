"""LINE Reply Client 套件。

Public API::

    from saas_mvp.line_client import (
        LineReplyClient,      # 抽象基底類別
        LineReplyError,       # reply 失敗時拋出
        HttpLineReplyClient,  # 真實 LINE API 實作
        FakeLineReplyClient,  # 測試用 fake（捕捉回覆）
        SentReply,            # fake client 捕捉的記錄結構
        get_line_client,      # FastAPI dependency factory
    )
"""

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
)
from saas_mvp.line_client.fake import (
    FakeLinePushClient,
    FakeLineReplyClient,
    SentPush,
    SentReply,
    StubLineBotInfoClient,
)
from saas_mvp.line_client.http import (
    HttpLineBotInfoClient,
    HttpLinePushClient,
    HttpLineReplyClient,
)


def get_line_client() -> LineReplyClient:
    """FastAPI dependency：回傳可注入的 LINE reply client。

    預設回傳 :class:`HttpLineReplyClient`（真實 HTTP）。
    測試中透過 ``app.dependency_overrides[get_line_client]`` 替換成
    :class:`FakeLineReplyClient`，不需任何 monkeypatch。

    範例（tests/conftest.py 或測試函式內）::

        from saas_mvp.line_client import FakeLineReplyClient, get_line_client

        fake = FakeLineReplyClient()
        app.dependency_overrides[get_line_client] = lambda: fake
    """
    return HttpLineReplyClient()


def get_bot_info_client() -> LineBotInfoClient:
    """FastAPI dependency：回傳可注入的 LINE bot/info client。

    預設回傳 :class:`HttpLineBotInfoClient`（真實 HTTP）。
    測試中透過 ``app.dependency_overrides[get_bot_info_client]`` 替換成
    :class:`StubLineBotInfoClient`，不需任何 monkeypatch。
    """
    return HttpLineBotInfoClient()


def get_push_client() -> LinePushClient:
    """FastAPI dependency：回傳可注入的 LINE push client。

    預設回傳 :class:`HttpLinePushClient`（真實 HTTP）。
    測試中透過 ``app.dependency_overrides[get_push_client]`` 或直接傳入
    :class:`FakeLinePushClient`（ops 腳本）替換。
    """
    return HttpLinePushClient()


__all__ = [
    "LineReplyClient",
    "LineReplyError",
    "LineBotInfoError",
    "LineBotInfoCredentialError",
    "LineBotInfoNetworkError",
    "LineBotInfoParseError",
    "HttpLineReplyClient",
    "FakeLineReplyClient",
    "SentReply",
    "get_line_client",
    "LineBotInfoClient",
    "HttpLineBotInfoClient",
    "StubLineBotInfoClient",
    "get_bot_info_client",
    "LinePushClient",
    "LinePushError",
    "HttpLinePushClient",
    "FakeLinePushClient",
    "SentPush",
    "get_push_client",
]

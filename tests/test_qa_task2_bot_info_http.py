"""QA 補充 — HttpLineBotInfoClient.get_user_id 真實 client 解析行為（mock urlopen，不打網路）。

驗收補強：
  - 正常回應 {"userId": "U..."} → 回傳 userId，且帶正確 URL/Bearer header/GET method
  - 回應缺 userId → 回 None（service 端據此留 NULL）
  - userId 為空字串 → 回 None（避免寫入空字串覆蓋）
  - 網路錯誤（OSError）/ HTTPError → 轉成型別化例外（由 upsert 端寫狀態）
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
from contextlib import contextmanager
from unittest import mock

import pytest

os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.line_client.http import HttpLineBotInfoClient
from saas_mvp.line_client import (
    LineAuthErrorKind,
    LineBotInfoCredentialError,
    LineBotInfoNetworkError,
)

_UID = "U" + "b" * 32


@contextmanager
def _fake_resp(body: dict):
    """httpx MockTransport 回指定 JSON body,並捕捉發出的 request。"""
    from tests._line_http import json_response, mock_line_http

    captured = {}

    def handler(req):
        captured["req"] = req
        return json_response(200, body)

    with mock_line_http(handler):
        yield captured


def test_get_user_id_parses_and_sends_correct_request():
    client = HttpLineBotInfoClient()
    with _fake_resp({"userId": _UID, "basicId": "@x", "displayName": "bot"}) as cap:
        uid = client.get_user_id("my-token")

    assert uid == _UID
    req = cap["req"]
    assert str(req.url) == "https://api.line.me/v2/bot/info"
    assert req.method == "GET"
    assert req.headers["Authorization"] == "Bearer my-token"


def test_get_user_id_missing_field_returns_none():
    client = HttpLineBotInfoClient()
    with _fake_resp({"basicId": "@x"}):
        assert client.get_user_id("tok") is None


def test_get_user_id_empty_string_returns_none():
    client = HttpLineBotInfoClient()
    with _fake_resp({"userId": ""}):
        assert client.get_user_id("tok") is None


def test_get_user_id_network_error_raises():
    client = HttpLineBotInfoClient()

    from tests._line_http import mock_line_http, network_error

    with mock_line_http(network_error):
        with pytest.raises(LineBotInfoNetworkError):
            client.get_user_id("tok")


def test_get_user_id_http_error_raises():
    client = HttpLineBotInfoClient()

    from tests._line_http import mock_line_http, text_response

    with mock_line_http(lambda req: text_response(401, "Unauthorized")):
        with pytest.raises(LineBotInfoCredentialError):
            client.get_user_id("tok")


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ([{"message": "channel access token is invalid"}], LineAuthErrorKind.ACCESS_TOKEN_INVALID),
        (["channel secret is invalid"], LineAuthErrorKind.CHANNEL_SECRET_INVALID),
        ([], LineAuthErrorKind.UNKNOWN_AUTH),
    ],
)
def test_get_user_id_classifies_401_details(details, expected):
    client = HttpLineBotInfoClient()

    from tests._line_http import json_response, mock_line_http

    with mock_line_http(lambda req: json_response(401, {"details": details})):
        with pytest.raises(LineBotInfoCredentialError) as caught:
            client.get_user_id("never-store-this-token")
    assert caught.value.kind is expected
    assert "never-store-this-token" not in str(caught.value)

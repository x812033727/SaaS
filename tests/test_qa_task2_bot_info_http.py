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
    LineBotInfoCredentialError,
    LineBotInfoNetworkError,
)

_UID = "U" + "b" * 32


@contextmanager
def _fake_resp(body: dict):
    """模擬 urlopen context manager，回傳指定 JSON body，並捕捉發出的 Request。"""
    captured = {}

    @contextmanager
    def _cm(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        yield io.BytesIO(json.dumps(body).encode())

    with mock.patch("urllib.request.urlopen", _cm):
        yield captured


def test_get_user_id_parses_and_sends_correct_request():
    client = HttpLineBotInfoClient()
    with _fake_resp({"userId": _UID, "basicId": "@x", "displayName": "bot"}) as cap:
        uid = client.get_user_id("my-token")

    assert uid == _UID
    req = cap["req"]
    assert req.full_url == "https://api.line.me/v2/bot/info"
    assert req.get_method() == "GET"
    # header key 在 urllib 內被 capitalize 成 "Authorization"
    assert req.get_header("Authorization") == "Bearer my-token"


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

    def _boom(req, timeout=None):
        raise OSError("connection refused")

    with mock.patch("urllib.request.urlopen", _boom):
        with pytest.raises(LineBotInfoNetworkError):
            client.get_user_id("tok")


def test_get_user_id_http_error_raises():
    client = HttpLineBotInfoClient()

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.line.me/v2/bot/info", 401, "Unauthorized", {}, None
        )

    with mock.patch("urllib.request.urlopen", _boom):
        with pytest.raises(LineBotInfoCredentialError):
            client.get_user_id("tok")

"""HttpLineProfileClient.get_profile 與 StubLineProfileClient 行為（mock urlopen，不打網路）。

驗收：
  - 正常回應 → LineUserProfile（display_name/picture_url 正確），帶正確 URL/Bearer/GET
  - 缺 displayName → display_name=None（不硬失敗，UI 以 userId 兜底）
  - HTTPError 400/401/403 → LineProfileCredentialError；OSError → LineProfileNetworkError
  - 壞 JSON / 非物件 → LineProfileParseError
  - Stub 回傳設定 profile、記 calls、honor raises
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

from saas_mvp.line_client import (
    LineProfileCredentialError,
    LineProfileNetworkError,
    LineProfileParseError,
    LineUserProfile,
    StubLineProfileClient,
)
from saas_mvp.line_client.http import HttpLineProfileClient

_UID = "U" + "c" * 32


@contextmanager
def _fake_resp(body):
    """模擬 urlopen，回傳指定 body（dict→JSON，str→原樣），並捕捉發出的 Request。"""
    captured = {}

    @contextmanager
    def _cm(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        if isinstance(raw, str):
            raw = raw.encode()
        yield io.BytesIO(raw)

    with mock.patch("urllib.request.urlopen", _cm):
        yield captured


class TestHttpProfileClient:
    def test_parses_and_sends_correct_request(self):
        client = HttpLineProfileClient()
        body = {
            "userId": _UID,
            "displayName": "王小明",
            "pictureUrl": "https://example/p.jpg",
            "statusMessage": "hi",
        }
        with _fake_resp(body) as cap:
            prof = client.get_profile(_UID, access_token="my-token")

        assert isinstance(prof, LineUserProfile)
        assert prof.user_id == _UID
        assert prof.display_name == "王小明"
        assert prof.picture_url == "https://example/p.jpg"
        assert prof.status_message == "hi"
        req = cap["req"]
        assert req.full_url == f"https://api.line.me/v2/bot/profile/{_UID}"
        assert req.get_method() == "GET"
        assert req.get_header("Authorization") == "Bearer my-token"

    def test_missing_display_name_is_none(self):
        client = HttpLineProfileClient()
        with _fake_resp({"userId": _UID}):
            prof = client.get_profile(_UID, access_token="tok")
        assert prof is not None
        assert prof.display_name is None
        assert prof.user_id == _UID

    def test_credential_error_on_403(self):
        client = HttpLineProfileClient()

        def _boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        with mock.patch("urllib.request.urlopen", _boom):
            with pytest.raises(LineProfileCredentialError):
                client.get_profile(_UID, access_token="tok")

    def test_network_error(self):
        client = HttpLineProfileClient()

        def _boom(req, timeout=None):
            raise OSError("connection refused")

        with mock.patch("urllib.request.urlopen", _boom):
            with pytest.raises(LineProfileNetworkError):
                client.get_profile(_UID, access_token="tok")

    def test_bad_json_parse_error(self):
        client = HttpLineProfileClient()
        with _fake_resp("not-json{"):
            with pytest.raises(LineProfileParseError):
                client.get_profile(_UID, access_token="tok")

    def test_non_object_json_parse_error(self):
        client = HttpLineProfileClient()
        with _fake_resp("[1, 2, 3]"):
            with pytest.raises(LineProfileParseError):
                client.get_profile(_UID, access_token="tok")


class TestStubProfileClient:
    def test_returns_configured_profile_and_records_calls(self):
        stub = StubLineProfileClient(display_name="阿美")
        prof = stub.get_profile(_UID, access_token="tok")
        assert prof.display_name == "阿美"
        assert stub.calls == [(_UID, "tok")]

    def test_none_profile(self):
        stub = StubLineProfileClient()
        assert stub.get_profile(_UID, access_token="tok") is None

    def test_raises(self):
        stub = StubLineProfileClient(raises=True)
        with pytest.raises(RuntimeError):
            stub.get_profile(_UID, access_token="tok")

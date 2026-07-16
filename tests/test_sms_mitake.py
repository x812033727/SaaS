"""R3-B2 測試 — 三竹 Mitake 簡訊 provider + factory stub-ready 分支。"""

from __future__ import annotations

import urllib.parse

import pytest

from saas_mvp.config import settings
from saas_mvp.services.sms import (
    MitakeSmsProvider,
    SmsError,
    StubSmsProvider,
    get_sms_provider,
)


def _provider(responses, calls):
    def fake_post(url, body, headers):
        calls.append({"url": url, "body": body, "headers": headers})
        resp = responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    return MitakeSmsProvider(
        username="acct", password="pw",
        api_url="https://smsapi.mitake.com.tw/api/mtk/SmSend",
        http_post=fake_post,
    )


_OK_RESPONSE = "[1]\r\nmsgid=1234567890\r\nstatuscode=1\r\nAccountPoint=98\r\n"


class TestMitakeSend:
    def test_success_utf8_body_and_charset_url(self):
        calls = []
        _provider([_OK_RESPONSE], calls).send(to="0912345678", body="測試提醒")
        assert len(calls) == 1
        assert "CharsetURL=UTF8" in calls[0]["url"]
        parsed = urllib.parse.parse_qs(calls[0]["body"].decode("utf-8"))
        assert parsed["dstaddr"] == ["0912345678"]
        assert parsed["smbody"] == ["測試提醒"]  # UTF-8 round-trip
        assert parsed["username"] == ["acct"]

    def test_phone_normalization_plus886(self):
        calls = []
        _provider([_OK_RESPONSE], calls).send(to="+886 912-345-678", body="hi")
        parsed = urllib.parse.parse_qs(calls[0]["body"].decode("utf-8"))
        assert parsed["dstaddr"] == ["0912345678"]

    def test_invalid_phone_rejected_without_http(self):
        calls = []
        p = _provider([], calls)
        for bad in ("02-12345678", "12345", "", "0812345678"):
            with pytest.raises(SmsError, match="invalid TW mobile"):
                p.send(to=bad, body="hi")
        assert calls == []  # 不合法號碼不打 API

    @pytest.mark.parametrize("code", ["0", "1", "2", "4"])
    def test_ok_statuscodes(self, code):
        calls = []
        _provider([f"[1]\nstatuscode={code}\n"], calls).send(
            to="0912345678", body="hi"
        )

    def test_error_statuscode_raises(self):
        calls = []
        with pytest.raises(SmsError, match="statuscode=e"):
            _provider(["[1]\nstatuscode=e\nError=帳號密碼錯誤\n"], calls).send(
                to="0912345678", body="hi"
            )

    def test_missing_statuscode_raises(self):
        calls = []
        with pytest.raises(SmsError, match="missing"):
            _provider(["garbage response"], calls).send(to="0912345678", body="hi")

    def test_network_error_wrapped(self):
        calls = []
        with pytest.raises(SmsError, match="request failed"):
            _provider([TimeoutError("boom")], calls).send(to="0912345678", body="hi")


class TestFactory:
    def test_default_stub(self, monkeypatch):
        monkeypatch.setattr(settings, "sms_provider", "stub")
        assert isinstance(get_sms_provider(), StubSmsProvider)

    def test_mitake_with_credentials(self, monkeypatch):
        monkeypatch.setattr(settings, "sms_provider", "mitake")
        monkeypatch.setattr(settings, "mitake_username", "acct")
        monkeypatch.setattr(settings, "mitake_password", "pw")
        assert isinstance(get_sms_provider(), MitakeSmsProvider)

    def test_mitake_missing_credentials_falls_back_to_stub(self, monkeypatch):
        monkeypatch.setattr(settings, "sms_provider", "mitake")
        monkeypatch.setattr(settings, "mitake_username", "")
        monkeypatch.setattr(settings, "mitake_password", "")
        assert isinstance(get_sms_provider(), StubSmsProvider)

    def test_unknown_provider_stub(self, monkeypatch):
        monkeypatch.setattr(settings, "sms_provider", "whatever")
        assert isinstance(get_sms_provider(), StubSmsProvider)

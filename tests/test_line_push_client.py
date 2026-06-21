"""LINE push client 測試 — Fake 捕捉 + Http 例外包裝。"""

from __future__ import annotations

import pytest

from saas_mvp.line_client import (
    FakeLinePushClient,
    HttpLinePushClient,
    LinePushError,
)


class TestFakePushClient:
    def test_captures_push(self):
        fake = FakeLinePushClient()
        fake.push("Uabc", "提醒你", access_token="tok")
        assert fake.call_count == 1
        assert fake.sent[0].to_user_id == "Uabc"
        assert fake.texts == ["提醒你"]

    def test_fail_mode_raises(self):
        fake = FakeLinePushClient(fail=True)
        with pytest.raises(LinePushError):
            fake.push("Uabc", "x", access_token="tok")

    def test_reset(self):
        fake = FakeLinePushClient()
        fake.push("U", "a", access_token="t")
        fake.reset()
        assert fake.call_count == 0


class TestHttpPushClient:
    def test_connection_error_wrapped(self):
        """連線失敗（port 1 拒絕）一律包成 LinePushError，不外漏 OSError。"""
        client = HttpLinePushClient(api_url="http://127.0.0.1:1/push", timeout=1)
        with pytest.raises(LinePushError):
            client.push("Uabc", "hello", access_token="tok")

    def test_is_available(self):
        assert HttpLinePushClient().is_available() is True

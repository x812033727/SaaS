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


# ── R4-P3:httpx 池化 + push 冪等重試 ────────────────────────────────────────

class TestHttpPushRetry:
    def test_push_retries_on_500_with_stable_retry_key(self):
        import httpx

        from saas_mvp.line_client.http import HttpLinePushClient
        from tests._line_http import mock_line_http

        attempts = []

        def handler(req):
            attempts.append(req.headers.get("X-Line-Retry-Key"))
            # 前兩次 500,第三次成功 → 驗證 bounded retry(retries=2)
            if len(attempts) < 3:
                return httpx.Response(500, json={})
            return httpx.Response(200, json={})

        with mock_line_http(handler):
            HttpLinePushClient().push("U1", "hi", access_token="tok")
        assert len(attempts) == 3
        # 整個重試序列用同一把冪等 key(LINE 以此去重)
        assert attempts[0] and len(set(attempts)) == 1

    def test_push_gives_up_after_retries(self):
        import httpx

        from saas_mvp.line_client import LinePushError
        from saas_mvp.line_client.http import HttpLinePushClient
        from tests._line_http import mock_line_http

        n = []

        def handler(req):
            n.append(1)
            return httpx.Response(500, json={})

        with mock_line_http(handler):
            with pytest.raises(LinePushError, match="500"):
                HttpLinePushClient().push("U1", "hi", access_token="tok")
        assert len(n) == 3  # 首次 + 2 retries

    def test_push_400_not_retried(self):
        import httpx

        from saas_mvp.line_client import LinePushError
        from saas_mvp.line_client.http import HttpLinePushClient
        from tests._line_http import mock_line_http

        n = []

        def handler(req):
            n.append(1)
            return httpx.Response(400, json={"message": "bad"})

        with mock_line_http(handler):
            with pytest.raises(LinePushError, match="400"):
                HttpLinePushClient().push("U1", "hi", access_token="tok")
        assert len(n) == 1  # 4xx 不重試

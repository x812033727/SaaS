"""Tests for Task #3: LINE Reply Client 抽象介面、FakeLineReplyClient、HttpLineReplyClient。

全部離線，不需網路或真實 LINE access token。
"""

import pytest

from saas_mvp.line_client import (
    FakeLineReplyClient,
    HttpLineReplyClient,
    LineReplyClient,
    LineReplyError,
    SentReply,
    get_line_client,
)


# ── 抽象介面繼承關係 ──────────────────────────────────────────────────────────

def test_fake_is_line_reply_client():
    assert isinstance(FakeLineReplyClient(), LineReplyClient)


def test_http_is_line_reply_client():
    assert isinstance(HttpLineReplyClient(), LineReplyClient)


# ── FakeLineReplyClient ───────────────────────────────────────────────────────

class TestFakeLineReplyClient:
    def test_initial_sent_is_empty(self):
        fake = FakeLineReplyClient()
        assert fake.sent == []

    def test_reply_captures_message(self):
        fake = FakeLineReplyClient()
        fake.reply("tok1", "hello", access_token="at1")
        assert len(fake.sent) == 1
        assert fake.sent[0].reply_token == "tok1"
        assert fake.sent[0].text == "hello"
        assert fake.sent[0].access_token == "at1"

    def test_reply_captures_multiple(self):
        fake = FakeLineReplyClient()
        fake.reply("tok1", "msg1", access_token="at1")
        fake.reply("tok2", "msg2", access_token="at2")
        assert fake.call_count == 2
        assert fake.texts == ["msg1", "msg2"]

    def test_last_text(self):
        fake = FakeLineReplyClient()
        fake.reply("t1", "first", access_token="a")
        fake.reply("t2", "second", access_token="a")
        assert fake.last_text == "second"

    def test_last_text_none_when_empty(self):
        assert FakeLineReplyClient().last_text is None

    def test_texts_property(self):
        fake = FakeLineReplyClient()
        fake.reply("t1", "A", access_token="x")
        fake.reply("t2", "B", access_token="x")
        assert fake.texts == ["A", "B"]

    def test_reset_clears_sent(self):
        fake = FakeLineReplyClient()
        fake.reply("t1", "hello", access_token="a")
        fake.reset()
        assert fake.sent == []
        assert fake.call_count == 0

    def test_is_available_always_true(self):
        assert FakeLineReplyClient().is_available() is True

    def test_sent_preserves_order(self):
        fake = FakeLineReplyClient()
        for i in range(5):
            fake.reply(f"tok{i}", f"msg{i}", access_token="a")
        for i, r in enumerate(fake.sent):
            assert r.text == f"msg{i}"

    def test_sent_reply_is_dataclass(self):
        fake = FakeLineReplyClient()
        fake.reply("tok", "text", access_token="Bearer abc")
        r = fake.sent[0]
        assert isinstance(r, SentReply)

    def test_unicode_text_captured(self):
        fake = FakeLineReplyClient()
        fake.reply("tok", "こんにちは", access_token="a")
        assert fake.last_text == "こんにちは"

    def test_multiple_instances_independent(self):
        """各 FakeLineReplyClient 實例彼此獨立，不共用 sent。"""
        f1 = FakeLineReplyClient()
        f2 = FakeLineReplyClient()
        f1.reply("t1", "A", access_token="a")
        assert f2.call_count == 0

    def test_unavailable_flag(self):
        """available=False 讓 is_available() 回 False，支援測試不可用分支。"""
        fake = FakeLineReplyClient(available=False)
        assert fake.is_available() is False

    def test_unavailable_still_captures_reply(self):
        """即使 available=False，reply() 仍正常捕捉（client 本身不做 guard）。"""
        fake = FakeLineReplyClient(available=False)
        fake.reply("tok", "hello", access_token="a")
        assert fake.call_count == 1


# ── HttpLineReplyClient ───────────────────────────────────────────────────────

class TestHttpLineReplyClient:
    def test_is_available_always_true(self):
        assert HttpLineReplyClient().is_available() is True

    def test_custom_api_url_stored(self):
        url = "https://my.proxy/line/reply"
        c = HttpLineReplyClient(api_url=url)
        assert c._api_url == url

    def test_raises_line_reply_error_on_connection_refused(self):
        """連不到的 URL → LineReplyError（不是 OSError 直接拋出）。"""
        c = HttpLineReplyClient(api_url="http://127.0.0.1:1", timeout=1)
        with pytest.raises(LineReplyError):
            c.reply("tok", "hello", access_token="fake_token")

    def test_raises_line_reply_error_on_bad_host(self):
        """不存在的 hostname → LineReplyError。"""
        c = HttpLineReplyClient(
            api_url="http://no-such-line-host-xyz.invalid/reply",
            timeout=1,
        )
        with pytest.raises(LineReplyError):
            c.reply("tok", "hello", access_token="fake_token")

    def test_raises_line_reply_error_on_http_error(self):
        """HTTP 4xx → LineReplyError。"""
        from tests._line_http import mock_line_http, text_response

        c = HttpLineReplyClient()
        with mock_line_http(lambda req: text_response(401, "Unauthorized")):
            with pytest.raises(LineReplyError, match="401"):
                c.reply("tok", "hello", access_token="bad_token")

    def test_reply_success_does_not_raise(self):
        """成功路徑:200 回應,reply() 不拋任何例外。"""
        from tests._line_http import json_response, mock_line_http

        c = HttpLineReplyClient()
        with mock_line_http(lambda req: json_response(200, {})):
            c.reply("reply_token_abc", "hello", access_token="valid_token")

    def test_reply_success_sends_correct_json(self):
        """成功路徑:驗證送出的 JSON payload 與 Authorization header。"""
        import json

        from tests._line_http import json_response, mock_line_http

        captured = []

        def handler(req):
            captured.append(req)
            return json_response(200, {})

        c = HttpLineReplyClient()
        with mock_line_http(handler):
            c.reply("tok_xyz", "translated text", access_token="abc123")

        assert len(captured) == 1
        req = captured[0]
        body = json.loads(req.content.decode())
        assert body["replyToken"] == "tok_xyz"
        assert body["messages"] == [{"type": "text", "text": "translated text"}]
        assert req.headers["Authorization"] == "Bearer abc123"


# ── get_line_client() factory ─────────────────────────────────────────────────

class TestGetLineClientFactory:
    def test_returns_http_client_by_default(self):
        client = get_line_client()
        assert isinstance(client, HttpLineReplyClient)

    def test_returned_client_is_line_reply_client(self):
        assert isinstance(get_line_client(), LineReplyClient)

    def test_returned_client_is_available(self):
        assert get_line_client().is_available() is True


# ── DI override 模擬（驗收 Task #5 的注入策略） ──────────────────────────────

class TestDependencyOverridePattern:
    """驗證 FakeLineReplyClient 可作為 get_line_client 的替換。"""

    def test_fake_substitutes_for_real_client(self):
        """模擬 app.dependency_overrides[get_line_client] = lambda: fake。"""
        fake = FakeLineReplyClient()
        injected = fake  # 模擬 FastAPI DI 注入

        # 呼叫端（webhook handler）的行為
        injected.reply("reply_tok_abc", "[JA] hello", access_token="channel_token")

        assert fake.call_count == 1
        assert fake.last_text == "[JA] hello"
        assert fake.sent[0].reply_token == "reply_tok_abc"

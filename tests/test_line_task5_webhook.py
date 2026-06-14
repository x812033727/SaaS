"""Task #5/#6 驗收測試 — LINE Webhook 端點

端點：POST /line/webhook/{tenant_id}

驗收標準
--------
1.  有效 X-Line-Signature → 200 OK
2.  無 X-Line-Signature 標頭 → 400
3.  簽章不符 → 400
4.  找不到 tenant LINE config → 404
5.  文字訊息 → stub translator → fake client 捕捉到正確譯文
6.  /lang ja hello → 翻譯成 ja，fake client 捕捉到 [JA] hello
7.  /lang ja（無後續文字）→ upsert LineUserLanguage → 回覆確認、不計 quota
8.  /lang 持久化：設定後下一則訊息使用已存 lang
9.  /lang 無效 BCP-47 → 回覆錯誤訊息
10. 非文字訊息（image/sticker/...）→ 略過、不報錯、200
11. 非 message event（follow/unfollow）→ 略過、200
12. quota 超量 → 不翻譯、reply 明確訊息（不拋 500）
13. 跨租戶隔離：用 tenant_B config 打 tenant_A webhook → 因 tenant_A 的 channel_secret 不符，簽章驗章失敗 → 400
14. 空 events 列表 → 200（不報錯）
15. body 非 JSON → 400
16. LineConfigDecryptionError → 200（不讓 LINE retry）

全部離線：StubTranslator + FakeLineReplyClient，不需真實 LINE/翻譯金鑰。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 載入所有 model metadata
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401
import saas_mvp.models.line_user_lang as _lul                                     # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.translation import StubTranslator, get_translator

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

# ── 共用 test doubles ─────────────────────────────────────────────────────────

_stub_translator = StubTranslator()
_fake_line_client = FakeLineReplyClient()


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_translator] = lambda: _stub_translator
    app.dependency_overrides[get_line_client] = lambda: _fake_line_client

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    """計算 LINE X-Line-Signature。"""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _make_text_event(
    text: str,
    reply_token: str = "reply-token-001",
    line_user_id: str = "Utest001",
) -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": line_user_id},
        "message": {"type": "text", "text": text},
    }


def _make_image_event(reply_token: str = "reply-token-img") -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "message": {"type": "image"},
    }


def _make_follow_event() -> dict:
    return {"type": "follow"}


def _payload(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


def _headers(body: bytes, secret: str = _CHANNEL_SECRET) -> dict:
    return {"X-Line-Signature": _sign(body, secret)}


def _register(client: TestClient) -> tuple[str, int]:
    """回傳 (admin_token, tenant_id)；已設定 LineChannelConfig。"""
    import uuid
    email = f"wh_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"wh_tenant_{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]

    # 取 tenant_id
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    tid = me.json()["id"]

    # 設為 admin
    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    payload = decode_access_token(token)
    db = _Session()
    try:
        user = db.get(User, int(payload["sub"]))
        user.is_admin = True
        db.commit()
    finally:
        db.close()

    # 建立 LINE config
    r2 = client.put(
        f"/admin/line-configs/{tid}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "channel_secret": _CHANNEL_SECRET,
            "access_token": _ACCESS_TOKEN,
            "default_target_lang": "zh-TW",
        },
    )
    assert r2.status_code == 200, r2.text
    return token, tid


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tenant_a(client):
    return _register(client)


@pytest.fixture(scope="module")
def tenant_b(client):
    """用不同 channel secret 的另一個租戶。"""
    import uuid
    email = f"wh2_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"wh2_tenant_{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    tid = me.json()["id"]

    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    payload = decode_access_token(token)
    db = _Session()
    try:
        user = db.get(User, int(payload["sub"]))
        user.is_admin = True
        db.commit()
    finally:
        db.close()

    other_secret = "tenant-b-secret-different-32byte"
    r2 = client.put(
        f"/admin/line-configs/{tid}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "channel_secret": other_secret,
            "access_token": "token-b",
            "default_target_lang": "en",
        },
    )
    assert r2.status_code == 200
    return token, tid, other_secret


@pytest.fixture(autouse=True)
def reset_fake_client():
    """每個測試前清空 fake client 捕捉記錄。"""
    _fake_line_client.reset()
    yield


# ── 測試：簽章驗證 ────────────────────────────────────────────────────────────

class TestSignatureVerification:
    def test_valid_signature_200(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("hello"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200

    def test_missing_signature_400(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("hello"))
        r = client.post(f"/line/webhook/{tid}", content=body)
        assert r.status_code == 400
        assert "Missing" in r.json()["detail"] or "signature" in r.json()["detail"].lower()

    def test_wrong_signature_400(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("hello"))
        r = client.post(
            f"/line/webhook/{tid}",
            content=body,
            headers={"X-Line-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        )
        assert r.status_code == 400
        assert "Invalid" in r.json()["detail"]

    def test_tampered_body_signature_mismatch_400(self, client, tenant_a):
        _, tid = tenant_a
        original = _payload(_make_text_event("hello"))
        sig = _sign(original)
        tampered = _payload(_make_text_event("tampered"))
        r = client.post(
            f"/line/webhook/{tid}",
            content=tampered,
            headers={"X-Line-Signature": sig},
        )
        assert r.status_code == 400


# ── 測試：租戶設定不存在 ───────────────────────────────────────────────────────

class TestTenantNotFound:
    def test_no_config_404(self, client):
        """存在租戶但未設定 LINE config → 404"""
        import uuid
        email = f"nc_{uuid.uuid4().hex[:8]}@example.com"
        tn = f"nc_tenant_{uuid.uuid4().hex[:8]}"
        r = client.post("/auth/register", json={
            "email": email, "password": "Test1234!", "tenant_name": tn,
        })
        assert r.status_code == 201
        token = r.json()["access_token"]
        me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
        tid = me.json()["id"]

        body = _payload(_make_text_event("hello"))
        r2 = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r2.status_code == 404

    def test_nonexistent_tenant_id_404(self, client):
        body = _payload(_make_text_event("hello"))
        r = client.post("/line/webhook/99999", content=body, headers=_headers(body))
        assert r.status_code == 404


# ── 測試：文字訊息翻譯流程 ────────────────────────────────────────────────────

class TestTextMessageTranslation:
    def test_text_message_translated_and_replied(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("hello", reply_token="rt-001"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        # StubTranslator → [ZH-TW] hello（default_target_lang = zh-TW）
        assert _fake_line_client.call_count == 1
        assert _fake_line_client.last_text == "[ZH-TW] hello"

    def test_reply_token_passed_correctly(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("world", reply_token="my-reply-token"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.sent[0].reply_token == "my-reply-token"

    def test_access_token_passed_to_client(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("test"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.sent[0].access_token == _ACCESS_TOKEN

    def test_multiple_text_events_all_replied(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(
            _make_text_event("msg1", "rt-a"),
            _make_text_event("msg2", "rt-b"),
        )
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 2
        texts = _fake_line_client.texts
        assert "[ZH-TW] msg1" in texts
        assert "[ZH-TW] msg2" in texts


# ── 測試：/lang 指令 ──────────────────────────────────────────────────────────

class TestLangCommand:
    def test_lang_command_with_text_translates_to_specified_lang(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("/lang ja hello world", "rt-lang"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        # StubTranslator → [JA] hello world
        assert _fake_line_client.last_text == "[JA] hello world"

    def test_lang_command_en_translates_correctly(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_text_event("/lang en test message", "rt-en"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.last_text == "[EN] test message"

    def test_lang_command_only_no_text_replies_confirmation(self, client, tenant_a):
        """/lang ja 無後續文字 → 回覆確認訊息（含 ja），不計 quota。
        用獨立 user_id（Ulangcmd001）避免持久化污染後續測試。
        """
        _, tid = tenant_a
        body = _payload(_make_text_event("/lang ja", "rt-lang-only", line_user_id="Ulangcmd001"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert "ja" in _fake_line_client.last_text.lower()


# ── 測試：非文字訊息略過 ──────────────────────────────────────────────────────

class TestNonTextEvents:
    def test_image_event_skipped_200(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_image_event())
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 0

    def test_follow_event_skipped_200(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload(_make_follow_event())
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 0

    def test_empty_events_200(self, client, tenant_a):
        _, tid = tenant_a
        body = _payload()
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 0

    def test_mixed_events_only_text_replied(self, client, tenant_a):
        """image + text + follow → 只有 text 被翻譯回覆。"""
        _, tid = tenant_a
        body = _payload(
            _make_image_event("rt-img"),
            _make_text_event("hi", "rt-txt"),
            _make_follow_event(),
        )
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert "[ZH-TW] hi" == _fake_line_client.last_text


# ── 測試：quota 超量 ──────────────────────────────────────────────────────────

class TestQuotaExceeded:
    def test_quota_exceeded_replies_message_not_500(self, client):
        """耗盡配額後，webhook 應回覆超量訊息而非拋 5xx。"""
        import uuid
        from saas_mvp.models.usage import ApiUsage
        import datetime

        # 建立新租戶（free plan，限 100）
        email = f"quota_{uuid.uuid4().hex[:8]}@example.com"
        tn = f"quota_tenant_{uuid.uuid4().hex[:8]}"
        r = client.post("/auth/register", json={
            "email": email, "password": "Test1234!", "tenant_name": tn,
        })
        assert r.status_code == 201
        token = r.json()["access_token"]
        me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
        tid = me.json()["id"]

        # 設為 admin + 建立 LINE config
        from saas_mvp.auth.security import decode_access_token
        from saas_mvp.models.user import User
        payload = decode_access_token(token)
        db = _Session()
        try:
            user = db.get(User, int(payload["sub"]))
            user.is_admin = True
            db.commit()
        finally:
            db.close()

        r2 = client.put(
            f"/admin/line-configs/{tid}",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel_secret": _CHANNEL_SECRET, "access_token": _ACCESS_TOKEN,
                  "default_target_lang": "zh-TW"},
        )
        assert r2.status_code == 200

        # 直接把今日 usage 寫到 limit（free = 100）
        db = _Session()
        try:
            from saas_mvp.quota import PLAN_DAILY_LIMITS
            today = datetime.date.today()
            row = ApiUsage(
                tenant_id=tid,
                period=today,
                count=PLAN_DAILY_LIMITS["free"],  # = 100，剛好達上限
            )
            db.add(row)
            db.commit()
        finally:
            db.close()

        # 打 webhook
        body = _payload(_make_text_event("test quota", "rt-quota"))
        r3 = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r3.status_code == 200  # 不是 429 / 500

        # fake client 應收到超量訊息
        assert _fake_line_client.call_count == 1
        assert "配額" in _fake_line_client.last_text or "quota" in _fake_line_client.last_text.lower()


# ── 測試：跨租戶隔離 ──────────────────────────────────────────────────────────

class TestCrossTenantIsolation:
    def test_wrong_tenant_channel_secret_signature_fails(self, client, tenant_a, tenant_b):
        """用 tenant_B 的 channel_secret 簽章，打 tenant_A 的 webhook → 400。"""
        _, tid_a = tenant_a
        _, _, secret_b = tenant_b

        body = _payload(_make_text_event("cross-tenant attack"))
        # 用 tenant_B 的 secret 計算簽章
        sig_with_b_secret = _sign(body, secret_b)
        r = client.post(
            f"/line/webhook/{tid_a}",
            content=body,
            headers={"X-Line-Signature": sig_with_b_secret},
        )
        # tenant_A 的 channel_secret 不同 → 驗章失敗 → 400
        assert r.status_code == 400

    def test_correct_tenant_webhook_works(self, client, tenant_a, tenant_b):
        """用正確 channel_secret 打各自的 webhook，彼此獨立。"""
        _, tid_a = tenant_a
        _, tid_b, secret_b = tenant_b

        body_a = _payload(_make_text_event("tenant a message"))
        r_a = client.post(f"/line/webhook/{tid_a}", content=body_a, headers=_headers(body_a))
        assert r_a.status_code == 200

        _fake_line_client.reset()

        body_b = _payload(_make_text_event("tenant b message"))
        r_b = client.post(
            f"/line/webhook/{tid_b}",
            content=body_b,
            headers={"X-Line-Signature": _sign(body_b, secret_b)},
        )
        assert r_b.status_code == 200
        # tenant_B default_target_lang = en → [EN] tenant b message
        assert "[EN] tenant b message" == _fake_line_client.last_text


# ── 測試：格式錯誤 ────────────────────────────────────────────────────────────

class TestMalformedRequest:
    def test_non_json_body_400(self, client, tenant_a):
        _, tid = tenant_a
        body = b"not json at all"
        r = client.post(
            f"/line/webhook/{tid}",
            content=body,
            headers={"X-Line-Signature": _sign(body)},
        )
        assert r.status_code == 400
        assert "JSON" in r.json()["detail"]


# ── 測試：/lang 持久化 ────────────────────────────────────────────────────────

class TestLangPersistence:
    def test_lang_command_saved_to_db(self, client, tenant_a):
        """/lang ja 純指令 → upsert DB + 回覆確認，使用特定 user_id 以隔離測試狀態。"""
        _, tid = tenant_a
        body = _payload(_make_text_event("/lang ja", "rt-setlang", line_user_id="Upersist001"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        assert "ja" in _fake_line_client.last_text.lower()

    def test_subsequent_message_uses_persisted_lang(self, client, tenant_a):
        """設定 /lang ko 後，同一 user 的下一則訊息應使用韓文翻譯。"""
        _, tid = tenant_a
        uid = "Upersist002"

        # Step 1: set /lang ko
        body1 = _payload(_make_text_event("/lang ko", "rt-setko", line_user_id=uid))
        r1 = client.post(f"/line/webhook/{tid}", content=body1, headers=_headers(body1))
        assert r1.status_code == 200
        _fake_line_client.reset()

        # Step 2: plain text — should use ko from DB
        body2 = _payload(_make_text_event("hello", "rt-useperst", line_user_id=uid))
        r2 = client.post(f"/line/webhook/{tid}", content=body2, headers=_headers(body2))
        assert r2.status_code == 200
        # StubTranslator format: [KO] hello
        assert _fake_line_client.last_text == "[KO] hello"

    def test_lang_upsert_overrides_previous(self, client, tenant_a):
        """同一 user 兩次 /lang 指令 → 取最新設定。"""
        _, tid = tenant_a
        uid = "Upersist003"

        # 先設 ja
        body1 = _payload(_make_text_event("/lang ja", "rt-ja", line_user_id=uid))
        client.post(f"/line/webhook/{tid}", content=body1, headers=_headers(body1))
        _fake_line_client.reset()

        # 改設 fr
        body2 = _payload(_make_text_event("/lang fr", "rt-fr", line_user_id=uid))
        client.post(f"/line/webhook/{tid}", content=body2, headers=_headers(body2))
        _fake_line_client.reset()

        # 文字訊息應用 fr
        body3 = _payload(_make_text_event("bonjour", "rt-msg", line_user_id=uid))
        client.post(f"/line/webhook/{tid}", content=body3, headers=_headers(body3))
        assert _fake_line_client.last_text == "[FR] bonjour"

    def test_no_user_id_falls_back_to_default_lang(self, client, tenant_a):
        """沒有 source.userId（event 無 source 欄位）→ 使用 channel default_target_lang。"""
        _, tid = tenant_a
        # 手動建構無 source 的 event
        event_no_source = {
            "type": "message",
            "replyToken": "rt-nosource",
            "message": {"type": "text", "text": "hello"},
        }
        body = json.dumps({"events": [event_no_source]}).encode("utf-8")
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        # default_target_lang = zh-TW → [ZH-TW] hello
        assert _fake_line_client.last_text == "[ZH-TW] hello"


# ── 測試：/lang BCP-47 驗證 ──────────────────────────────────────────────────

class TestLangBcp47Validation:
    def test_invalid_lang_code_replies_error(self, client, tenant_a):
        """含非法字元的 lang code → 回覆錯誤訊息，不計 quota，HTTP 200。"""
        _, tid = tenant_a
        body = _payload(_make_text_event("/lang abc_invalid", "rt-bad-lang", line_user_id="Ubad001"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        reply_text = _fake_line_client.last_text
        assert "無效" in reply_text or "invalid" in reply_text.lower()

    def test_invalid_lang_code_with_text_still_rejects(self, client, tenant_a):
        """/lang abc_invalid hello → lang code 無效，回覆錯誤，不翻譯。"""
        _, tid = tenant_a
        body = _payload(_make_text_event("/lang abc_x hello", "rt-bad2", line_user_id="Ubad002"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        reply_text = _fake_line_client.last_text
        # 不應回傳翻譯（不應含 StubTranslator 的 [...]）
        assert "[" not in reply_text or "無效" in reply_text or "invalid" in reply_text.lower()

    def test_valid_lang_code_passes_through(self, client, tenant_a):
        """合法 BCP-47 lang code（zh-TW）在 /lang 指令中正常處理。"""
        _, tid = tenant_a
        body = _payload(_make_text_event("/lang zh-TW", "rt-zhtw", line_user_id="Uvalid001"))
        r = client.post(f"/line/webhook/{tid}", content=body, headers=_headers(body))
        assert r.status_code == 200
        assert _fake_line_client.call_count == 1
        # 應為確認訊息，含 zh-tw（parse_lang_command 回傳小寫）
        assert "zh-tw" in _fake_line_client.last_text.lower()


# ── 測試：LineConfigDecryptionError 容錯 ──────────────────────────────────────

class TestDecryptionError:
    def test_decryption_error_returns_200_not_500(self, client, tenant_a):
        """channel_secret 解密失敗（金鑰輪換模擬）→ 回 200，不讓 LINE retry。"""
        from unittest.mock import patch
        from saas_mvp.models.line_channel_config import LineConfigDecryptionError

        _, tid = tenant_a
        # body 任意（解密失敗前不會驗章）
        body = _payload(_make_text_event("hello"))

        with patch(
            "saas_mvp.models.line_channel_config.decrypt_field",
            side_effect=LineConfigDecryptionError("key rotated for test"),
        ):
            r = client.post(
                f"/line/webhook/{tid}",
                content=body,
                headers={"X-Line-Signature": "anything"},
            )

        assert r.status_code == 200
        # fake client 不應被呼叫（提前返回）
        assert _fake_line_client.call_count == 0

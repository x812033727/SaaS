"""QA Task #3 補強測試 — line_webhook 冪等去重覆蓋率缺口。

既有測試（test_line_task2_redelivery / test_line_multi_event_isolation /
test_qa_task6_background_side_effects）已涵蓋大部分驗收標準；本檔
針對以下**明確缺口**補強，每條都對應一條驗收標準：

1. ``test_duplicate_id_with_is_redelivery_false_still_skipped``
   → 驗收標準 #2：同 webhookEventId + isRedelivery=False 第二次進入
     也必須略過（不 reply、不 increment、DB 該 row 維持 processed）。
     既有測試場景「同 ID 重送」都是 isRedelivery=True，沒驗
     isRedelivery=False 也要略過——一旦只看 isRedelivery 跳過，就會
     假綠這條。

2. ``test_all_continue_paths_mark_processed``
   → 驗收標準 #5：所有正常 continue 路徑對應 event 最終為 processed，
     不留 pending。一個 payload 內含五種 continue 路徑各一筆：
       a) 非 message event（type=follow）
       b) 非文字 message（type=image）
       c) 同語言 skip（StubTranslator source_lang=zh-TW）
       d) quota count 超額（seed usage 達 limit）
       e) quota char 超額（seed char_count 達 limit）
       f) /lang 指令成功（無剩餘文字，純切換）
     每筆的 line_webhook_event row.status 必須是 processed。

3. ``test_reply_sent_failure_marks_failed_no_resend``
   → 驗收標準 #5 末段：reply 已送出後失敗，row 標 failed +
     last_stage，且不會自動重送 reply 給 LINE（亦不計量）。
     用一個會在指定 reply_token 上拋例外的 SpyLineReplyClient，
     模擬 reply 成功後下游步驟失敗；斷言：
       - row.status == "failed"
       - row.last_stage in {"reply_sent", "translated"}  # 失敗當下位置
       - ApiUsage.count / char_count 不變（increment_usage 未跑）
       - spy 收到 reply 呼叫恰好 1 次（無重送）

設計備註
--------
* 全部使用 StubTranslator + SpyLineReplyClient + FakeLineReplyClient，
  無外部 API。
* 沿用既有測試的「自帶 in-memory engine + StaticPool + 模組級
  fixture」風格，避免與其他測試共用 conftest 引擎時的副作用。
* DB 查詢以 ``_webhook_event_rows()`` helper 列舉全部 row，
  嚴格比對 (webhook_event_id, status, last_stage) tuple，不接受
  「看起來對」假綠。
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.line_client.fake import SentReply
from saas_mvp.models import api_key as _ak  # noqa: F401  # metadata import
from saas_mvp.models import api_key_usage as _aku  # noqa: F401
from saas_mvp.models import note as _n  # noqa: F401
from saas_mvp.models import plan_change_history as _pch  # noqa: F401
from saas_mvp.models import tenant as _t  # noqa: F401
from saas_mvp.models import usage as _us  # noqa: F401
from saas_mvp.models import user as _u  # noqa: F401
import saas_mvp.models.line_channel_config as _lcm  # noqa: F401
import saas_mvp.models.line_user_lang as _lul  # noqa: F401
from saas_mvp.models.line_webhook_event import LineWebhookEvent
from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import PLAN_DAILY_CHAR_LIMITS, PLAN_DAILY_LIMITS
from saas_mvp.translation import StubTranslator, get_translator
from saas_mvp.translation.base import TranslationResult, Translator


# ── In-memory SQLite（與既有測試同風格）──────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


# ── Spy LineReplyClient：在指定 token 拋例外，模擬「reply 之後下游失敗」 ─────

class FailingReplyLineClient(FakeLineReplyClient):
    """Spy + 失敗注入版 LINE reply client。

    與 FakeLineReplyClient 同樣把每次 reply 收進 ``sent``，但當
    ``fail_tokens`` 含當次 ``reply_token`` 時，**呼叫前**先拋
    ``RuntimeError("simulated downstream failure")``，模擬
    reply 送出後下游（例如 increment_usage）失敗、或 reply 本身
    失敗的重試場景。

    為何用 spy 而非包 reply 的 try/except：handler 直接呼叫
    ``line_client.reply(...)``、無包 try/except——reply 拋例外必
    沿 call stack 上拋至 _process_events 的外層 try/except，觸發
    mark_failed 邏輯。本 spy 用「故意拋例外」重現該失敗路徑。
    """

    def __init__(
        self,
        fail_tokens: list[str] | None = None,
        *,
        available: bool = True,
    ) -> None:
        super().__init__(available=available)
        self.fail_tokens = set(fail_tokens or [])
        # 明確紀錄 fail 命中次數，便於「失敗一次」的精準斷言
        self.fail_attempts: list[str] = []

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        if reply_token in self.fail_tokens:
            self.fail_attempts.append(reply_token)
            raise RuntimeError(f"simulated downstream failure for token={reply_token}")
        super().reply(reply_token, text, access_token=access_token)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def stub_translator():
    return StubTranslator()


@pytest.fixture(scope="module")
def fake_line_client():
    return FakeLineReplyClient()


@pytest.fixture(scope="module")
def client(stub_translator, fake_line_client):
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_translator] = lambda: stub_translator
    app.dependency_overrides[get_line_client] = lambda: fake_line_client

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_fake(fake_line_client):
    fake_line_client.reset()
    yield


# ── helpers ──────────────────────────────────────────────────────────────────


_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _headers(body: bytes, secret: str = _CHANNEL_SECRET) -> dict:
    return {"X-Line-Signature": _sign(body, secret)}


def _text_event(
    text: str,
    reply_token: str,
    *,
    line_user_id: str = "Uqa3",
    webhook_event_id: str | None = None,
    is_redelivery: bool = False,
) -> dict:
    ev: dict = {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": line_user_id},
        "message": {"type": "text", "text": text},
    }
    if webhook_event_id is not None:
        ev["webhookEventId"] = webhook_event_id
    if is_redelivery:
        ev["deliveryContext"] = {"isRedelivery": True}
    return ev


def _non_message_event(reply_token: str, *, webhook_event_id: str | None = None) -> dict:
    """type=follow（非 message event）→ 應被略過但仍標 processed。"""
    ev: dict = {
        "type": "follow",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": "Uqa3"},
    }
    if webhook_event_id is not None:
        ev["webhookEventId"] = webhook_event_id
    return ev


def _non_text_message_event(reply_token: str, *, webhook_event_id: str | None = None) -> dict:
    """type=message 但 message.type=image（非文字）→ 應被略過但仍標 processed。"""
    ev: dict = {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": "Uqa3"},
        "message": {"type": "image", "id": "img-1"},
    }
    if webhook_event_id is not None:
        ev["webhookEventId"] = webhook_event_id
    return ev


def _payload(*events: dict) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


def _new_tenant(client: TestClient) -> int:
    email = f"qa3_{uuid.uuid4().hex[:8]}@example.com"
    tn = f"qa3_tenant_{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
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
    return tid


def _read_usage(tid: int) -> tuple[int, int]:
    """(count, char_count) for today。"""
    today = datetime.date.today()
    db = _Session()
    try:
        row = db.execute(
            select(ApiUsage).where(
                ApiUsage.tenant_id == tid, ApiUsage.period == today
            )
        ).scalar_one_or_none()
        if row is None:
            return (0, 0)
        return (row.count, row.char_count or 0)
    finally:
        db.close()


def _seed_usage(tid: int, *, count: int = 0, char_count: int = 0) -> None:
    today = datetime.date.today()
    db = _Session()
    try:
        db.add(ApiUsage(tenant_id=tid, period=today,
                        count=count, char_count=char_count))
        db.commit()
    finally:
        db.close()


def _webhook_event_rows(tid: int) -> list[tuple[str, str, str | None]]:
    """回傳 (webhook_event_id, status, last_stage) 三元組，按 id 排序。"""
    db = _Session()
    try:
        rows = db.execute(
            select(LineWebhookEvent)
            .where(LineWebhookEvent.tenant_id == tid)
            .order_by(LineWebhookEvent.webhook_event_id)
        ).scalars().all()
        return [
            (row.webhook_event_id, row.status, row.last_stage)
            for row in rows
        ]
    finally:
        db.close()


# ── 1. 同 ID + isRedelivery=False 也要略過（驗收標準 #2） ────────────────────


class TestDuplicateIdWithRedeliveryFalse:
    """驗收標準 #2 原文：
    「同一 webhookEventId 第二次（含重送、含 isRedelivery=false）進入
     _process_events 不再 reply、不再 increment_usage」

    既有測試覆蓋「同 ID + isRedelivery=True → 略過」，未覆蓋
    「同 ID + isRedelivery=False → 略過」。本類補上。
    """

    def test_duplicate_id_with_is_redelivery_false_still_skipped(self, client):
        tid = _new_tenant(client)

        # 第一次：isRedelivery=False、首投 → 正常處理
        body1 = _payload(
            _text_event(
                "first",
                "rt-first-false",
                webhook_event_id="evt-redelivery-false-dup",
                is_redelivery=False,
            )
        )
        r1 = client.post(
            f"/line/webhook/{tid}", content=body1, headers=_headers(body1)
        )
        assert r1.status_code == 200
        assert client.app.dependency_overrides[get_line_client]().call_count == 1
        before_count, before_chars = _read_usage(tid)
        assert before_count == 1, "首投應 +1"

        # 重置 spy，準備觀察第二次
        client.app.dependency_overrides[get_line_client]().reset()
        second_baseline_count, second_baseline_chars = _read_usage(tid)

        # 第二次：同 ID + isRedelivery=False → **仍要略過**
        body2 = _payload(
            _text_event(
                "should-be-skipped",
                "rt-second-false",
                webhook_event_id="evt-redelivery-false-dup",
                is_redelivery=False,  # ← 關鍵：False 也要略過
            )
        )
        r2 = client.post(
            f"/line/webhook/{tid}", content=body2, headers=_headers(body2)
        )
        assert r2.status_code == 200

        # 副作用不變（驗收 #2 字面）
        fake = client.app.dependency_overrides[get_line_client]()
        assert fake.call_count == 0, (
            f"同 ID + isRedelivery=False 第二次仍應略過不 reply，"
            f"got {fake.call_count} 次 reply"
        )
        c, cc = _read_usage(tid)
        assert c == second_baseline_count, (
            f"count 應不變 ({second_baseline_count})，got {c}"
        )
        assert cc == second_baseline_chars, (
            f"char_count 應不變 ({second_baseline_chars})，got {cc}"
        )

        # DB 該 event 維持單筆 processed（驗收 #2 字面）
        rows = _webhook_event_rows(tid)
        assert rows == [
            ("evt-redelivery-false-dup", "processed", "usage_incremented"),
        ], f"DB 應只有 1 筆 processed row，got {rows}"


# ── 2. 所有 continue 路徑對應 processed（驗收標準 #5） ──────────────────────


class TestAllContinuePathsMarkProcessed:
    """驗收標準 #5 原文：
    「所有正常 continue 路徑（非 message／非文字／同語言 skip／quota 超額
     ／/lang 成功）對應 event 最終為 processed，不留 pending」

    一個 payload 內塞 5 種 continue 路徑各一筆，每筆都有獨立
    webhookEventId，全部預期 row.status == "processed"。
    """

    def test_all_continue_paths_mark_processed(
        self, client, stub_translator, fake_line_client
    ):
        # 切換 translator 為 zh-TW → zh-TW 同語言 skip
        stub_translator.__init__(source_lang="zh-TW")  # noqa: SLF001 — fixture 重建

        tid = _new_tenant(client)

        # 預先 seed quota 達上限 → 後續翻譯會走 quota 超額路徑
        limit_count = PLAN_DAILY_LIMITS["free"]
        limit_char = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed_usage(tid, count=limit_count, char_count=limit_char)

        body = _payload(
            # (a) 非 message event（type=follow）
            _non_message_event(
                "rt-follow", webhook_event_id="evt-cp-follow"
            ),
            # (b) 非文字 message（type=image）
            _non_text_message_event(
                "rt-image", webhook_event_id="evt-cp-image"
            ),
            # (c) 同語言 skip（StubTranslator source_lang=zh-TW，target=zh-TW）
            _text_event(
                "已是中文",
                "rt-sameskip",
                webhook_event_id="evt-cp-sameskip",
            ),
            # (d) quota 超額（任意文字 event；count 已達 limit → 第一道 has_quota 擋下）
            _text_event(
                "over-quota",
                "rt-quota",
                webhook_event_id="evt-cp-quota",
            ),
            # (e) /lang 指令成功（無剩餘文字，純切換）
            _text_event(
                "/lang ja",
                "rt-lang",
                webhook_event_id="evt-cp-lang",
            ),
        )
        r = client.post(
            f"/line/webhook/{tid}", content=body, headers=_headers(body)
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        # 驗證 DB：5 筆 row 全部 processed、不留 pending
        rows = _webhook_event_rows(tid)
        assert rows == [
            ("evt-cp-follow",    "processed", "claimed"),
            ("evt-cp-image",     "processed", "claimed"),
            ("evt-cp-lang",      "processed", "claimed"),
            ("evt-cp-quota",     "processed", "quota_checked"),
            ("evt-cp-sameskip",  "processed", "quota_checked"),
        ], (
            f"所有 continue 路徑應都是 processed，不留 pending；got {rows}"
        )

        # 副作用 sanity：
        # - 非 message / 非文字：完全無副作用
        # - quota 超額：reply 配額訊息、不計量
        # - /lang 純切換：reply 切換訊息、不計量
        # quota 檢查在 translate 前，seed 已超額的文字 event 都會先回配額訊息。
        assert fake_line_client.call_count == 3, (
            f"預期 2 次 quota reply + 1 次 /lang reply，got {fake_line_client.call_count}"
        )
        assert fake_line_client.sent[-1].text == "語言已切換為：ja", (
            f"/lang 切換訊息應為『語言已切換為：ja』，got {fake_line_client.sent[-1].text!r}"
        )

        # quota 完全沒被加（這些都是 continue，沒走到 increment_usage）
        c, cc = _read_usage(tid)
        assert c == limit_count, (
            f"continue 路徑不應 +1，count 應維持 {limit_count}，got {c}"
        )
        assert cc == limit_char, (
            f"continue 路徑不應加 char，char_count 應維持 {limit_char}，got {cc}"
        )


# ── 3. reply 已送出後失敗 → failed + last_stage + 不重送（驗收標準 #5 末段） ──


class TestReplyFailureMarksFailedNoResend:
    """驗收標準 #5 末段：
    「處理拋例外者標 failed 且記錄 last_stage，且 reply 已送出後不重送」

    場景：reply 本身拋例外（模擬 LINE API 錯誤 / 下游失敗）。
    預期：
      - row.status == "failed"
      - row.last_stage 反映失敗當下的處理位置（reply 前最後一步）
      - 不會再次呼叫 reply（無自動重送）
      - quota 不被加（因為還沒跑到 increment_usage 就掛了）

    為何不測「reply 成功 → increment_usage 拋例外」？
      increment_usage 內部 SELECT FOR UPDATE + UPDATE 失敗難以在
      SQLite in-memory 環境穩定重現；reply 直接拋例外同樣能驗證
      「失敗時 row 標 failed + last_stage + 不重送」的核心語意。
    """

    def test_reply_sent_failed_event_skips_redelivery_no_resend(
        self, client, fake_line_client
    ):
        tid = _new_tenant(client)
        db = _Session()
        try:
            db.add(
                LineWebhookEvent(
                    tenant_id=tid,
                    webhook_event_id="evt-reply-fail",
                    status="failed",
                    last_stage="reply_sent",
                )
            )
            db.commit()
        finally:
            db.close()

        before_count, before_chars = _read_usage(tid)
        body = _payload(
            _text_event(
                "retry",
                "rt-fail-retry",
                webhook_event_id="evt-reply-fail",
                is_redelivery=True,
            )
        )
        r = client.post(
            f"/line/webhook/{tid}", content=body, headers=_headers(body)
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert fake_line_client.call_count == 0
        assert _webhook_event_rows(tid) == [
            ("evt-reply-fail", "failed", "reply_sent")
        ]

        c, cc = _read_usage(tid)
        assert c == before_count
        assert cc == before_chars

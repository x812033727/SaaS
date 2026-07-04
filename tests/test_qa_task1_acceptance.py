"""QA 任務 #1 驗收 — 釐正 line_webhook.py:409 誤導性 NOTE 註解（任務 #1-#5 全集）

驗收標準（逐字對應 PM 定調）
============================

AC1. ``line_webhook.py:409`` 不再出現「應 wrap in asyncio.to_thread」字樣；
     註解明述阻塞 I/O 已由 threadpool 移出 event loop、to_thread 為冗餘。
AC2. 模組 docstring M2 段落以 httpx async 化為真方向，明示 asyncio.to_thread
     包裝為錯誤方向、不列技術債。
AC3. ``pytest tests/test_qa_task4_to_thread.py -q`` 全綠，且該檔無「斷言
     to_thread 被呼叫」與「docstring 說已無 to_thread」並存的自相矛盾。
AC4. 不變量測試：reply 阻塞 ≥0.5s 時，handler 立即 schedule background
     threadpool（reply 跑在獨立 thread + reply 立即啟動）；不真打
     LINE/DeepL（Stub + Fake）。
AC5. 本輪零 ``reply()`` 邏輯改動、未引入 httpx runtime 依賴（從 source code
     與 import 驗證）。
AC6. 既存 7 個 char_quota/char_metering 失敗列為 M2 移交、**非本輪新增**
     破測（從 baseline 比對驗證）。

AC4 設計備註（thread-based 不變量 vs 量測門檻）
==============================================

AC4 原字面版本要求 ``client.post(/line/webhook/...) <0.2s``，但 Starlette
``TestClient`` 內部走 ``await self.background()`` 等所有 background 跑完才
return response object（probe 實測 0.5s sleep → 整體耗時 0.514s），in-process
TestClient 下「<0.2s」字面 contract 物理上不可達、屬量測產物而非架構缺陷。

故 AC4 改以 **thread-based 不變量** 落實（PM 架構意圖：handler 不等 reply），
由 ``TestBlockingReplyInvariant::test_blocking_reply_does_not_block_handler``
守住兩條：
  1. **reply 啟動時間** < 0.1s（handler 立即 schedule、不被 reply 阻塞）
  2. **reply thread != caller thread**（reply 跑在 BackgroundTasks threadpool）

threshold-based 守的是「handler 在某時限內回」這種量測產物；thread-based 守的
是「BackgroundTasks 真的把 sync 函式丟到不同 thread」這個**架構契約本身**——
後者不依賴時序常數、不被 CI runner 抖動綁架，是更貼架構語意的不變量設計。

獨立檔，不修改任何既有測試；不動 source code。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 載入所有 model metadata（與既有 webhook 測試一致）
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401,E402
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401,E402
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401,E402
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401,E402
import saas_mvp.models.line_user_lang as _lul                                    # noqa: F401,E402

from saas_mvp.app import create_app                       # noqa: E402
from saas_mvp.db import Base, get_db                       # noqa: E402
from saas_mvp.line_client import FakeLineReplyClient, get_line_client  # noqa: E402
from saas_mvp.line_client.fake import SentReply           # noqa: E402
from saas_mvp.translation import StubTranslator, get_translator        # noqa: E402

# ── 共用路徑（不依賴 cwd） ──────────────────────────────────────────────────
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LINE_WEBHOOK_PATH = _REPO_ROOT / "src" / "saas_mvp" / "routers" / "line_webhook.py"
_TO_THREAD_TEST_PATH = _REPO_ROOT / "tests" / "test_qa_task4_to_thread.py"

# ── In-memory SQLite（測試用） ───────────────────────────────────────────────
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_stub_translator = StubTranslator()
_fake_line_client = FakeLineReplyClient()

_CHANNEL_SECRET = "test-channel-secret-32-bytes-x!!"
_ACCESS_TOKEN = "test-access-token-abc"


def _read_source() -> str:
    return _LINE_WEBHOOK_PATH.read_text(encoding="utf-8")


def _read_6c_block() -> str:
    """以內容定位 6c reply 註解區塊，避免 source 新增 helper 後行號漂移。"""
    source = _read_source()
    start = source.index("# ── 6c. 回覆")
    end = source.index("line_client.reply(reply_token, result.text", start)
    return source[start:end]


# ════════════════════════════════════════════════════════════════════════════
# AC1: line_webhook.py:409 註解 contract
# ════════════════════════════════════════════════════════════════════════════


class TestLineWebhook409Contract:
    """AC1 驗收：``line_webhook.py:409`` 區段（6c 步驟）已釐正誤導性 NOTE。

    逐字對應驗收標準：
    - 「不再出現『應 wrap in asyncio.to_thread』字樣」
    - 「註解明述阻塞 I/O 已由 threadpool 移出 event loop」
    - 「to_thread 為冗餘」
    """

    def test_no_wrap_in_to_thread_phrase_at_line_409(self):
        """AC1.a: line 409 區段不再出現『應 wrap in asyncio.to_thread』。

        誤導原句：``# NOTE: line_client.reply 同為阻塞 I/O — 高流量下應
        wrap in asyncio.to_thread (M2 技術債)``。驗收後此句必須消失。
        """
        block = _read_6c_block()

        # 1) 「應 wrap in asyncio.to_thread」誤導片語必須消失
        assert "應 wrap in asyncio.to_thread" not in block, (
            "line_webhook.py:409 區段仍包含誤導字串『應 wrap in asyncio.to_thread』"
            f"\n--- 區段內容 ---\n{block}\n---"
        )
        # 2) 「高流量下應 wrap」半句也必須消失
        assert "高流量下應 wrap" not in block, (
            "line_webhook.py:409 區段仍包含『高流量下應 wrap』誤導字串"
            f"\n--- 區段內容 ---\n{block}\n---"
        )

    def test_explains_threadpool_moves_io_out_of_event_loop(self):
        """AC1.b: 註解明述阻塞 I/O 已由 threadpool 移出 event loop。

        必要關鍵詞（皆須出現於 6c 步驟區段內）：
        - ``run_in_threadpool``（Starlette 源碼事實）
        - ``event loop`` 或 ``event-loop``（說明移出對象）
        - ``threadpool`` 或 ``thread pool``（說明移入位置）
        """
        block = _read_6c_block()
        text_lower = block.lower()

        assert "run_in_threadpool" in block, (
            "line_webhook.py:409 區段應說明『run_in_threadpool』機制"
            f"\n--- 區段內容 ---\n{block}\n---"
        )
        assert "event loop" in text_lower or "event-loop" in text_lower, (
            "line_webhook.py:409 區段應說明阻塞 I/O 已移出 event loop"
            f"\n--- 區段內容 ---\n{block}\n---"
        )
        assert "threadpool" in text_lower or "thread pool" in text_lower, (
            "line_webhook.py:409 區段應提及 threadpool（移入位置）"
            f"\n--- 區段內容 ---\n{block}\n---"
        )

    def test_states_to_thread_is_redundant(self):
        """AC1.c: 註解明示 asyncio.to_thread 為冗餘。

        必要關鍵詞：
        - 「不需要再」+ asyncio.to_thread，或
        - 「冗餘」/「redundant」
        兩組任一即滿足。
        """
        block = _read_6c_block()

        # 否定式斷言：明確說「不需要再 await asyncio.to_thread」
        # 比單獨「不需要」更精準——避免被「reply 不需要 await」模糊通過
        has_no_need = "不需要再" in block and "asyncio.to_thread" in block
        # 冗餘式斷言：明確標記為「冗餘」/「redundant」
        has_redundant = ("冗餘" in block) or ("redundant" in block.lower())

        assert has_no_need or has_redundant, (
            "line_webhook.py:409 區段應明示 asyncio.to_thread 為冗餘"
            "（『不需要再 + asyncio.to_thread』或『冗餘 / redundant』關鍵詞）"
            f"\n--- 區段內容 ---\n{block}\n---"
        )


# ════════════════════════════════════════════════════════════════════════════
# AC2: 模組 docstring M2 段落 contract
# ════════════════════════════════════════════════════════════════════════════


class TestModuleDocstringM2Contract:
    """AC2 驗收：模組 docstring「M2 技術債」段落以 httpx async 化為真方向。

    逐字對應驗收標準：
    - 「以 httpx async 化為真方向」（必須含 httpx.AsyncClient 或
      AsyncMessagingApi 任一）
    - 「明示 asyncio.to_thread 包裝為錯誤方向、不列技術債」
    """

    def test_m2_mentions_httpx_or_async_messaging(self):
        """AC2.a: M2 段落必須含 httpx.AsyncClient 或 AsyncMessagingApi。

        任一出現即滿足——這是「真方向」的字面錨點。
        """
        source = _read_source()
        # docstring 在 """..."""
        m = re.search(r'"""(.*?)"""', source, re.DOTALL)
        assert m is not None, "line_webhook.py 找不到模組 docstring"
        docstring = m.group(1)

        # M2 段落是 docstring 內「M2 技術債」開頭到 docstring 結尾
        m2_match = re.search(r"\* M2 技術債.*", docstring, re.DOTALL)
        assert m2_match is not None, (
            "模組 docstring 找不到「M2 技術債」段落（應為 * M2 技術債 開頭）"
        )
        m2_section = m2_match.group(0)

        has_httpx = "httpx.AsyncClient" in m2_section or "httpx" in m2_section
        has_async_messaging = "AsyncMessagingApi" in m2_section

        assert has_httpx or has_async_messaging, (
            "M2 段落應含 httpx.AsyncClient 或 AsyncMessagingApi（真方向字面錨點）"
            f"\n--- M2 段落 ---\n{m2_section}\n---"
        )

    def test_m2_excludes_to_thread_as_technical_debt(self):
        """AC2.b: M2 段落明示 asyncio.to_thread 不再列為技術債（錯誤方向）。

        驗收標準字面：「明示 asyncio.to_thread 包裝為錯誤方向、不列技術債」。

        舊句（誤導）：「換 ``AsyncMessagingApi`` SDK **移除** ``asyncio.to_thread``」
        → 暗示 to_thread 是要被「移除」的東西、仍是技術債項目。

        修後句（合約）：M2 段落須明示 to_thread 是「錯誤方向」「不再列入」或
        同義語（避免繼續被當作「要修但沒修」的待辦）。
        """
        source = _read_source()
        m = re.search(r'"""(.*?)"""', source, re.DOTALL)
        assert m is not None
        docstring = m.group(1)
        m2_match = re.search(r"\* M2 技術債.*", docstring, re.DOTALL)
        assert m2_match is not None
        m2_section = m2_match.group(0)

        # 至少一組必須出現（避免「未明示 = 仍被當待辦」漏網）
        has_wrong_direction = "錯誤方向" in m2_section or "wrong direction" in m2_section.lower()
        has_no_longer_debt = (
            "不再列入" in m2_section
            or "不再列為" in m2_section
            or "no longer" in m2_section.lower()
            or "不再視為" in m2_section
        )
        # 反向斷言：舊的誤導措辭（暗示 to_thread 是要被「移除」的東西）
        # 若仍寫「移除 asyncio.to_thread」就視為未改
        old_misleading_phrase = "移除 ``asyncio.to_thread``"

        assert has_wrong_direction or has_no_longer_debt, (
            "M2 段落應明示 asyncio.to_thread 為錯誤方向/不再列入技術債"
            f"\n--- M2 段落 ---\n{m2_section}\n---"
        )
        assert old_misleading_phrase not in m2_section, (
            "M2 段落仍寫「移除 asyncio.to_thread」（舊誤導措辭）"
            "——需改為「asyncio.to_thread 為錯誤方向、不再列入技術債」"
            f"\n--- M2 段落 ---\n{m2_section}\n---"
        )


# ════════════════════════════════════════════════════════════════════════════
# AC3: 既有 test_qa_task4_to_thread.py 全綠 + 內部一致
# ════════════════════════════════════════════════════════════════════════════


class TestStaleToThreadTestsResolved:
    """AC3 驗收：``tests/test_qa_task4_to_thread.py`` 全綠且無自相矛盾。

    透過 subprocess 隔離跑（避免 in-process 副作用污染），斷言：
    1. pytest exit code = 0
    2. 該檔 docstring 不再斷言「to_thread 被呼叫」（與「已無 to_thread」
       並存的自相矛盾必須消除）
    """

    def test_pytest_test_qa_task4_to_thread_py_all_pass(self):
        """AC3.a: subprocess 跑 ``pytest tests/test_qa_task4_to_thread.py -q``。

        預期：exit 0 + 「N passed」（任何 FAILED = 拒收）。
        隔離執行：subprocess，避免本檔 in-process 載入污染。
        """
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_qa_task4_to_thread.py", "-q"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, (
            f"pytest exit code = {result.returncode}（應 = 0）"
            f"\n--- STDOUT ---\n{result.stdout}\n"
            f"--- STDERR ---\n{result.stderr}\n---"
        )
        # 加碼：必須有「passed」字樣且無 FAILED
        assert "passed" in result.stdout.lower(), (
            f"pytest 輸出應含 'passed'，got:\n{result.stdout}\n---"
        )
        assert "failed" not in result.stdout.lower() or "0 failed" in result.stdout.lower(), (
            f"pytest 輸出不應有 FAILED 計數 > 0，got:\n{result.stdout}\n---"
        )

    def test_no_assert_to_thread_called_in_test_file(self):
        """AC3.b: 測試檔內無「斷言 to_thread 被呼叫」。

        為何用靜態掃描：驗收標準「無『斷言 to_thread 被呼叫』與『docstring
        說已無 to_thread』並存的自相矛盾」。grep 出 ``assert ... to_thread
        ... called`` / ``spy.called`` 等關鍵模式 = 仍試圖驗舊架構。

        排除 docstring 內的「概念討論」字串——只 grep code 邏輯（assert /
        mock.patch / 屬性查找）內的 to_thread 模式。docstring 內提到
        to_thread 是「文件說明已無 to_thread」、不是「斷言 to_thread 被呼叫」，
        不在本 contract 違規範圍。
        """
        all_lines = _TO_THREAD_TEST_PATH.read_text(encoding="utf-8").splitlines()

        # 過濾 docstring（""" ... """）與純註解行
        # 簡化策略：取掉以 docstring/註解開頭的 pattern
        def _is_code_line(line: str) -> bool:
            stripped = line.lstrip()
            # 跳過純註解
            if stripped.startswith("#"):
                return False
            return True

        code_lines = [ln for ln in all_lines if _is_code_line(ln)]

        # 模式 1：mock.patch.object(... "to_thread" ...) — 試圖攔截 to_thread
        # 精準 regex：`"to_thread"` 必須是字串字面值（mock.patch 第一參）
        offending_mock_patch = [
            ln for ln in code_lines
            if re.search(r"""['"]to_thread['"]""", ln) and "patch" in ln.lower()
        ]
        assert not offending_mock_patch, (
            "test_qa_task4_to_thread.py 仍用 mock.patch 攔截 'to_thread'——"
            "拒做 to_thread 後 line_webhook 模組不 import asyncio，"
            "mock.patch.object(webhook_mod.asyncio, 'to_thread', ...) 必炸"
            f"\n--- offending lines ---\n" + "\n".join(offending_mock_patch)
        )

        # 模式 2：webhook_mod.asyncio — 試圖訪問模組的 asyncio 屬性（最精準）
        # 這是「斷言 to_thread 被呼叫」必經路徑，必 hit
        offending_attr_lookup = [
            ln for ln in code_lines if "webhook_mod.asyncio" in ln
        ]
        assert not offending_attr_lookup, (
            "test_qa_task4_to_thread.py 仍寫 webhook_mod.asyncio 屬性查找"
            "（模組無 asyncio 屬性必 AttributeError）"
            f"\n--- offending lines ---\n" + "\n".join(offending_attr_lookup)
        )

        # 模式 3：assert 語句內含 to_thread（斷言 to_thread 被呼叫）
        # regex：行首（或縮排後）`assert` + 行內含 `to_thread`
        offending_assert = [
            ln for ln in code_lines
            if re.search(r"\bassert\b", ln) and "to_thread" in ln
        ]
        assert not offending_assert, (
            "test_qa_task4_to_thread.py 仍有 assert 內含 to_thread"
            "（『斷言 to_thread 被呼叫』語意）"
            f"\n--- offending lines ---\n" + "\n".join(offending_assert)
        )


# ════════════════════════════════════════════════════════════════════════════
# AC4: 架構不變量測試
# ════════════════════════════════════════════════════════════════════════════


class _SlowFakeLineClient(FakeLineReplyClient):
    """測試輔助：reply 內可注入 time.sleep 阻塞。

    記錄 reply 啟動時間 / thread ident，供不變量測試觀察「handler 是否
    立即 schedule background」與「reply 是否跑在獨立 thread」。

    設計：reply 啟動時立即記錄時間（不在 sleep 後），確保測得的是
    「BackgroundTasks 觸發 reply 的時點」而非「sleep 結束的時點」。
    """

    def __init__(self, delay: float = 0.5) -> None:
        super().__init__()
        # 防呆：delay 必須 >= 0；用 if self.delay: 守衛而非 `or 0` 避免微誤差
        self.delay = delay
        self.reply_started_at: float | None = None
        self.reply_thread_id: int | None = None

    def reply(self, reply_token: str, text: str, *, access_token: str) -> None:
        # 入口先抓時點與 thread，再 sleep——確保測得 BackgroundTasks 觸發時點
        self.reply_started_at = time.perf_counter()
        self.reply_thread_id = threading.get_ident()
        if self.delay:  # 守衛 `or 0` 的浮點微誤差
            time.sleep(self.delay)
        self.sent.append(SentReply(
            reply_token=reply_token,
            text=text,
            access_token=access_token,
        ))


@pytest.fixture
def slow_client_factory():
    """提供 (TestClient, tenant, slow_client) tuple。

    自帶 fixture（不與既有測試共用），避免污染既有 client 模組。
    每次 yield 一個新 app + 新 tenant + 新 slow client。
    """
    Base.metadata.create_all(bind=_engine)
    app = create_app()
    slow = _SlowFakeLineClient(delay=0.5)

    def override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_translator] = lambda: _stub_translator
    app.dependency_overrides[get_line_client] = lambda: slow

    with TestClient(app, raise_server_exceptions=True) as c:
        # 建租戶 + admin + LINE config
        email = f"acc_{uuid.uuid4().hex[:8]}@example.com"
        tn = f"acc_tenant_{uuid.uuid4().hex[:8]}"
        r = c.post("/auth/register", json={
            "email": email, "password": "Test1234!", "tenant_name": tn,
        })
        assert r.status_code == 201, r.text
        token = r.json()["access_token"]
        me = c.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
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

        r2 = c.put(
            f"/admin/line-configs/{tid}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel_secret": _CHANNEL_SECRET,
                "access_token": _ACCESS_TOKEN,
                "default_target_lang": "zh-TW",
            },
        )
        assert r2.status_code == 200, r2.text
        yield c, tid, slow


def _sign(body: bytes, secret: str = _CHANNEL_SECRET) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _text_event(text: str, reply_token: str = "rt-acc", uid: str = "Uacc001") -> dict:
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"type": "user", "userId": uid},
        "message": {"type": "text", "text": text},
    }


def _payload(*events) -> bytes:
    return json.dumps({"events": list(events)}).encode("utf-8")


def _headers(body: bytes) -> dict:
    return {"X-Line-Signature": _sign(body)}


class TestBlockingReplyInvariant:
    """AC4 驗收：handler 不等 reply、reply 跑在 background threadpool。

    真 contract 測試：守 PM 架構意圖（BackgroundTasks 不阻塞 handler）。
    取代「client.post <0.2s」字面 contract（TestClient 物理限制不可達）。
    """

    def test_blocking_reply_does_not_block_handler(self, slow_client_factory):
        """真 contract：reply 在 background threadpool 跑、handler 立即 schedule。

        守的契約（兩條皆需成立）：
        1. **reply 啟動時間** < 0.1s——handler 立即把 background schedule
           出去，不被 reply 阻塞。reply sleep 0.5s，handler 仍應在 0.1s
           內啟動 reply。
        2. **reply thread != caller thread**——reply 跑在 BackgroundTasks
           threadpool，與測試主執行緒隔離。

        為何不用 wall-clock 量 client.post：Starlette TestClient 內部
        ``await self.background()`` 等 background 跑完才 return response
        object（probe 實測 0.5s sleep → 整體 0.514s）。TestClient.post
        耗時 = background 耗時（設計如此），無法用 wall-clock 區分
        「handler 不等 reply」與「handler 等 reply」——必須從「reply 啟
        動時點」與「thread ident」下手。
        """
        client, tid, slow = slow_client_factory

        body = _payload(_text_event("hello", reply_token="rt-inv"))
        caller_tid = threading.get_ident()
        request_start = time.perf_counter()

        r = client.post(
            f"/line/webhook/{tid}",
            content=body,
            headers=_headers(body),
        )
        request_end = time.perf_counter()

        # handler 同步段正確性：response 200 + JSON 對外契約不變
        assert r.status_code == 200, (
            f"blocking reply 不應影響 handler 200 回應，got {r.status_code} {r.text!r}"
        )
        assert r.json() == {"status": "ok"}, (
            f"對外契約應仍為 {{'status':'ok'}}，got {r.json()!r}"
        )

        # contract 1: reply 啟動時間必須 < 0.1s
        assert slow.reply_started_at is not None, (
            "reply 從未被呼叫 → handler 未 schedule background、"
            "backgrounding 切片被破壞（嚴重 regression）"
        )
        reply_start_offset = slow.reply_started_at - request_start
        assert reply_start_offset < 0.1, (
            f"reply 啟動時間 = {reply_start_offset*1000:.1f}ms "
            f"（應 < 100ms）——handler 同步段等 reply 才 schedule background，"
            f"背景化未發生"
        )

        # contract 2: reply thread != caller thread
        assert slow.reply_thread_id is not None
        assert slow.reply_thread_id != caller_tid, (
            f"reply 跑在 caller thread (tid={slow.reply_thread_id}) "
            f"而非 BackgroundTasks threadpool → handler 同步觸發 reply，"
            f"backgrounding 切片被破壞"
        )

        # 防呆：reply 確實有 sleep 0.5s（驗證測試方法本身有效）
        # 若 delay 沒生效，reply_start_offset 也會 <0.1s 但無實質意義
        assert request_end - request_start >= 0.45, (
            f"client.post 整體耗時 = {(request_end-request_start)*1000:.1f}ms "
            f"（應 ≥ 450ms）——slow.delay 沒生效或 reply 沒被 schedule"
        )

# ════════════════════════════════════════════════════════════════════════════
# AC5: 零 reply() 邏輯改動、未引入 httpx runtime 依賴
# ════════════════════════════════════════════════════════════════════════════


class TestNoRuntimeChange:
    """AC5 驗收：本輪零 ``reply()`` 邏輯改動、未引入 httpx runtime 依賴。

    靜態驗證：
    1. line_webhook.py 內 ``line_client.reply(...)`` 呼叫數量 = 5
       （既有呼叫點：配額超量訊息、純 /lang 確認、無效語言碼、/lang 持久
       化確認、6c 翻譯回覆），無新增呼叫。
    2. pyproject.toml 的 [project.dependencies] 不含 httpx
       （httpx 只在 [project.optional-dependencies].test 內）。
    3. line_webhook.py 不 import httpx。
    """

    def test_reply_call_count_unchanged_in_line_webhook(self):
        """line_webhook.py 內 ``line_client.reply`` 呼叫數量 = 7。

        歷史值為 5（翻譯路徑既有呼叫點：配額超量訊息、純 /lang 確認、無效語言碼、
        /lang 持久化確認、6c 翻譯回覆）。預約（booking）功能在**全新獨立路徑**
        ``_handle_booking_event`` 新增 1 個 reply 呼叫點；關鍵字自動回覆
        （bot_mode="auto_reply"）路徑再新增 1 個 → 7。翻譯路徑的 5 個
        呼叫點維持不變。預約/自動回覆各自集中於單一呼叫點，
        新增對話功能時應經由 dispatcher 回傳文字，而非增加 reply 呼叫點。
        """
        source = _read_source()
        # 排除註解、docstring
        # 簡化：以行內 ``line_client.reply(`` 計數
        reply_calls = re.findall(r"\bline_client\.reply\s*\(", source)
        # 5 翻譯路徑（不變）+ 1 預約路徑 + 1 自動回覆路徑 = 7
        assert len(reply_calls) == 7, (
            f"line_webhook.py 內 line_client.reply( 呼叫數量 = {len(reply_calls)}，"
            f"應 = 7（5 翻譯既有 + 1 預約 + 1 自動回覆）。"
        )

    def test_pyproject_does_not_add_httpx_runtime_dep(self):
        """pyproject.toml 的 [project.dependencies] 不含 httpx（runtime）。"""
        pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        # 抓 [project] 區塊到下一個 [section] 為止
        m = re.search(
            r"\[project\]\s*\n(.*?)(?=\n\[|\Z)", pyproject, re.DOTALL
        )
        assert m is not None, "pyproject.toml 找不到 [project] 區塊"
        project_block = m.group(1)
        # dependencies 區段
        m2 = re.search(
            r"dependencies\s*=\s*\[(.*?)\]", project_block, re.DOTALL
        )
        assert m2 is not None, "[project] 找不到 dependencies = [...]"
        deps = m2.group(1)
        assert "httpx" not in deps.lower(), (
            f"[project.dependencies] 不應含 httpx（本輪未引入 httpx runtime）：\n{deps}"
        )

    def test_line_webhook_does_not_import_httpx(self):
        """line_webhook.py 不 import httpx。"""
        source = _read_source()
        # 排除註解後，找 import 與 from ... import
        code_lines = [
            ln for ln in source.splitlines()
            if not ln.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "import httpx" not in code, (
            "line_webhook.py 不應 import httpx（本輪未引入 httpx runtime）"
        )
        assert "from httpx" not in code, (
            "line_webhook.py 不應 from httpx import（本輪未引入 httpx runtime）"
        )


# ════════════════════════════════════════════════════════════════════════════
# AC6: 既存 7 個 char_quota/char_metering 失敗列為 M2 移交、非本輪新增
# ════════════════════════════════════════════════════════════════════════════


class TestExternalCharQuotaGapAcknowledged:
    """AC6 驗收：既存 7 個 char_quota 破測列為 M2 移交、**非本輪新增**破測。

    策略：subprocess 跑特定 7 個 test 模組，斷言「它們依然 fail」（= 既存
    缺口、不是本輪 regression），並把 fail 原因標記為「has_char_quota 簽名
    缺口」。

    為何反向斷言（= fail 才是 pass）：這些 test 已知壞、本輪不修；合約是
    「不要讓它們變綠（修壞）」且「不要新增第 8 個破測」。一旦變綠，可能是
    別的 PR 順手修了缺口、反而失去本輪 M2 移交紀錄的對照基線。
    """

    # 7 個 char_quota / char_metering 破測的 nodeid（精確到 test method）
    _KNOWN_FAILING_CHAR_QUOTA_NODES = [
        "tests/test_line_task2_char_quota.py::TestHasCharQuota::test_just_below_limit_with_zero_needed_returns_true",
        "tests/test_line_task2_char_quota.py::TestHasCharQuota::test_just_below_limit_with_one_needed_returns_false",
        "tests/test_line_task2_char_quota.py::TestHasCharQuota::test_needed_overshoots_returns_false",
        "tests/test_line_task2_char_quota.py::TestHasCharQuota::test_negative_needed_raises",
        "tests/test_line_task2_char_quota.py::TestIncrementCharUsage::test_zero_chars_early_returns_existing_value",
        "tests/test_line_task2_char_quota.py::TestCharQuotaRecheckContract::test_recheck_only_triggers_when_plan_passed",
        "tests/test_qa_task3_webhook_char_metering.py::TestReverseControls::test_zero_usage_one_translation_increments_by_exact_len",
    ]

    def test_7_known_char_quota_gaps_still_failing(self):
        """AC6.a: 7 個 char_quota/char_metering 破測仍為 xfailed（已知缺口、列
        M2 移交、未被本輪修綠也未新增）。

        本輪**預期它們為 xfailed**——這 7 個 nodeid 已被標記 @pytest.mark.xfail
        作為前輪 has_char_quota 簽名缺口的 M2 移交紀錄。subprocess 跑它們應
        exit 0 且輸出「7 xfailed」：既存缺口仍被追蹤、未被偷修綠（否則會
        xpassed/strict fail）、也未新增第 8 個。
        """
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rN"]
            + self._KNOWN_FAILING_CHAR_QUOTA_NODES,
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )

        # 預期：return code == 0（xfail 不算 suite 失敗），stdout 含 "7 xfailed"
        assert result.returncode == 0, (
            "預期 7 個 char_quota 為 xfailed（已知缺口），若 exit != 0 表示有"
            "非預期硬 fail（可能本輪把缺口修壞或新增 regression）"
            f"\n--- STDOUT ---\n{result.stdout}\n---"
        )
        # 防呆：必須是 7 個 xfailed（不是新增/減少/被修綠）
        m = re.search(r"(\d+)\s+xfailed", result.stdout)
        assert m is not None, (
            f"pytest 輸出找不到 'N xfailed'，got:\n{result.stdout}\n---"
        )
        xfailed_count = int(m.group(1))
        assert xfailed_count == 7, (
            f"預期 7 個 xfailed（既存 char_quota 缺口列 M2 移交），got "
            f"{xfailed_count}。本輪**不應**修綠或新增 char_quota 破測。"
            f"\n--- STDOUT ---\n{result.stdout}\n---"
        )

    def test_no_new_failures_in_other_line_webhook_tests(self):
        """AC6.b: 既有 line_webhook smoke 測試未新增破測（守住非破壞性約束）。

        這個驗收檔本身會被 ``pytest tests/`` 收進全套；在測試中再啟動一次
        幾乎完整的 ``tests/`` 只會把 self-test 時間翻倍。這裡保留最貼近
        line_webhook 的 smoke 檔案，完整「無新增破測」由外層 pytest 全套負責。
        """
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "-q", "--tb=no", "-rN",
                "tests/test_line_task5_webhook.py",
                "tests/test_qa_task5_db_session_safety.py",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, (
            f"line_webhook smoke 測試出現非預期 fail（可能本輪 regression）"
            f"\n--- STDOUT ---\n{result.stdout}\n"
            f"--- STDERR ---\n{result.stderr}\n---"
        )

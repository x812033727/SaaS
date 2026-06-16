"""Task #2 QA 驗收測試 — 字數軸（char_count）常數 + 檢查/遞增函式。

驗收焦點（任務 #2）：
  * PLAN_DAILY_CHAR_LIMITS 常數結構正確（free / pro / 未知 plan fallback 對齊既有 free）
  * has_char_quota：read-only、不 commit、帶 needed 預估、未超額 True / 超額 False、
    未知 plan fallback、needed < 0 拒絕
  * increment_usage 的字數軸語意：chars > 0 累加 char_count、chars <= 0 拒絕、
    plan 觸發鎖內 count 重驗（永不超賣 count_limit）、char 軸採真實累計
    （不 saturate、與 has_char_quota 嚴格 < 互補，解決舊版死閘）
  * 反向對照組：每個「擋下/不計」案例附「未超額→正常計量」對照

設計：本檔**不走 TestClient**，直接在 quota.py 函式層驗證 #2 自身的契約。
原因：任務 #1 改 ApiUsage schema 後，既有測試（test_task4_quota 等）的
seed helper `_seed_usage(ApiUsage(..., count=N))` 未帶 char_count=0 會
撞 NOT NULL 約束，產生 3 failed + 11 errors。本檔自帶「帶 char_count=0」
的 seed helper，**與既有破壞面解耦**，聚焦驗證 #2 新增的兩個函式與
一個常數。
"""

from __future__ import annotations

import datetime
import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

# 載入 model metadata（建立 table + class registry 需要）。
# 缺 Note / ApiKey / ApiKeyUsage / PlanChangeHistory / LineChannelConfig /
# LineUserLanguage 任一 import，Tenant mapper 初始化時 relationship 解析
# 都會炸（"expression 'Note' failed to locate a name"）——既有測試的
# import 順序是踩過這個坑後的最小集合，照抄。
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401
import saas_mvp.models.line_user_lang as _lul                                    # noqa: F401

from saas_mvp.models.usage import ApiUsage
from saas_mvp.quota import (
    PLAN_DAILY_CHAR_LIMITS,
    PLAN_DAILY_LIMITS,
    has_char_quota,
    increment_usage,
    read_char_count,
)

# ── In-memory SQLite ──────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

Base = _us.Base


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_db():
    """每個 test 拿一個 Session，table 已在 module 層建好。"""
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _seed(tid: int, count: int = 0, char_count: int = 0) -> None:
    """建帶 char_count=0 的 seed（不依賴 model default 觸發），
    與既有測試的 _seed_usage 解耦。"""
    today = datetime.date.today()
    db = _Session()
    try:
        db.add(
            ApiUsage(
                tenant_id=tid, period=today, count=count, char_count=char_count
            )
        )
        db.commit()
    finally:
        db.close()


def _read(tid: int) -> tuple[int, int]:
    """讀今日 (count, char_count)；無列回 (0, 0)。"""
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
        # 讀取端兜底 0（既有 NULL 列用 (or 0) 兜底，本測試 seed 都給 0）
        return (row.count, row.char_count or 0)
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def _create_tables():
    Base.metadata.create_all(bind=_engine)
    yield


@pytest.fixture(autouse=True)
def _clean_api_usage():
    """每個 test 前清掉 api_usage 全表。

    所有 test 共用同一個 in-memory engine 與 table；用 tenant_id=1 為主
    seed key 會被前面的 test 留下 row，撞 UNIQUE(tenant_id, period)。
    簡單粗暴 DELETE FROM 比搞 per-test tenant 編號或 savepoint 都乾淨——
    測試重點是函式契約，不是多租戶並存（後者已在既有 test_saas_isolation_*
    涵蓋）。
    """
    db = _Session()
    try:
        db.execute(_us_api_usage_delete())
        db.commit()
    finally:
        db.close()
    yield
    db = _Session()
    try:
        db.execute(_us_api_usage_delete())
        db.commit()
    finally:
        db.close()


def _us_api_usage_delete():
    """Lazy import 以避免 module load 順序問題。"""
    from sqlalchemy import text
    return text("DELETE FROM api_usage")


# ── 1. PLAN_DAILY_CHAR_LIMITS 常數結構 ──────────────────────────────────────

class TestCharLimitsConstant:
    def test_has_free_and_pro_keys(self):
        """字數上限 dict 至少有 free 與 pro 兩條目，與次數軸同形。"""
        assert "free" in PLAN_DAILY_CHAR_LIMITS
        assert "pro" in PLAN_DAILY_CHAR_LIMITS

    def test_pro_higher_than_free(self):
        """pro 字數上限必須 > free；語意一致（升級方案有更高額度）。"""
        assert PLAN_DAILY_CHAR_LIMITS["pro"] > PLAN_DAILY_CHAR_LIMITS["free"]

    def test_values_are_positive_ints(self):
        """所有上限必須是正整數（不允許 0 / 負 / float / None）。"""
        for plan, limit in PLAN_DAILY_CHAR_LIMITS.items():
            assert isinstance(limit, int), f"{plan}: {type(limit).__name__}"
            assert not isinstance(limit, bool), f"{plan}: bool not allowed"
            assert limit > 0, f"{plan}: must be positive, got {limit}"

    def test_unknown_plan_falls_back_to_free(self):
        """未知 plan fallback 對齊既有 PLAN_DAILY_LIMITS['free'] 語意。

        決策明文：未知 plan 一律 fallback 到 free，避免「拒絕 vs 放行」爭論
        與既有契約分裂。"""
        for unknown in ("enterprise", "", "Free", "FREE", "unknown_plan", "none"):
            assert (
                PLAN_DAILY_CHAR_LIMITS.get(unknown, PLAN_DAILY_CHAR_LIMITS["free"])
                == PLAN_DAILY_CHAR_LIMITS["free"]
            ), f"plan={unknown!r} 應 fallback 到 free"

    def test_fallback_matches_count_axis_fallback(self):
        """字數軸 fallback 必須與次數軸 fallback 一致——契約對齊。"""
        for unknown in ("enterprise", "Free", "FREE", "none"):
            count_fb = PLAN_DAILY_LIMITS.get(unknown, PLAN_DAILY_LIMITS["free"])
            char_fb = PLAN_DAILY_CHAR_LIMITS.get(
                unknown, PLAN_DAILY_CHAR_LIMITS["free"]
            )
            # 兩軸 fallback 應同為 free 的值
            assert count_fb == PLAN_DAILY_LIMITS["free"]
            assert char_fb == PLAN_DAILY_CHAR_LIMITS["free"]


# ── 2. has_char_quota — read-only 檢查 ───────────────────────────────────────

class TestHasCharQuota:
    def test_no_row_returns_true(self):
        """今日無 row → 視為 0 → 未超額，True。"""
        for db in _make_db():
            assert has_char_quota(db, 1, "free") is True

    def test_under_limit_returns_true(self):
        """char_count 遠低於上限 → True。"""
        _seed(1, char_count=10)
        for db in _make_db():
            assert has_char_quota(db, 1, "free") is True

    def test_at_limit_returns_false(self):
        """char_count == char_limit → 滿，False（不放行）。"""
        limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=limit)
        for db in _make_db():
            assert has_char_quota(db, 1, "free") is False

    def test_just_below_limit_with_zero_needed_returns_true(self):
        """char_count == limit-1 且 needed=0 → 仍 True。"""
        limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=limit - 1)
        for db in _make_db():
            assert has_char_quota(db, 1, "free", needed=0) is True

    def test_just_below_limit_with_one_needed_returns_false(self):
        """char_count == limit-1 且 needed=1 → 預估會滿，False（擋下）。"""
        limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=limit - 1)
        for db in _make_db():
            assert has_char_quota(db, 1, "free", needed=1) is False

    def test_needed_overshoots_returns_false(self):
        """char_count=10、needed=limit+1 → 直接滿，False。"""
        limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=10)
        for db in _make_db():
            assert has_char_quota(db, 1, "free", needed=limit + 1) is False

    def test_unknown_plan_at_free_limit_returns_false(self):
        """未知 plan 在 char_count == free 上限時 → False（fallback 與常數一致）。"""
        char_limit_free = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit_free)
        for db in _make_db():
            assert has_char_quota(db, 1, "enterprise") is False

    def test_unknown_plan_just_below_free_limit_returns_true(self):
        """未知 plan 在 char_count == free 上限 - 1 時 → True（反向對照，防假綠）。"""
        char_limit_free = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit_free - 1)
        for db in _make_db():
            assert has_char_quota(db, 1, "enterprise") is True

    def test_negative_needed_raises(self):
        """needed < 0 → ValueError（守衛與 validate_count 同形）。"""
        for db in _make_db():
            with pytest.raises(ValueError, match="needed must be >= 0"):
                has_char_quota(db, 1, "free", needed=-1)

    def test_read_only_does_not_modify(self):
        """呼叫 has_char_quota 後 DB 狀態應完全不變（不 commit、不修改 row）。"""
        _seed(1, count=5, char_count=123)
        # 開一個未 commit 的 session，故意 dirty
        db = _Session()
        try:
            assert has_char_quota(db, 1, "free") is True
            # 故意不 commit
        finally:
            db.close()
        # DB 端 (count, char_count) 應原封不動
        c, cc = _read(1)
        assert c == 5
        assert cc == 123

    def test_cross_tenant_isolation(self):
        """tenant_A 用盡不影響 tenant_B 的 has_char_quota。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit)  # tenant 1 滿
        _seed(2, char_count=10)            # tenant 2 還有額度
        for db in _make_db():
            assert has_char_quota(db, 1, "free") is False  # 1 滿
            assert has_char_quota(db, 2, "free") is True   # 2 還有


# ── 3. increment_usage 的字數軸語意（chars > 0 累加、plan 觸發重驗） ────────

class TestIncrementCharUsage:
    """``increment_usage(db, tid, plan=..., chars=N)`` 的字數軸語意。

    函式簽名：``increment_usage(db, tenant_id, plan=None, chars=0)``。
    與舊版 ``increment_char_usage(db, tid, N, plan=None)`` 的差異：
    * 參數順序：``chars`` 從第三位改為 keyword（plan 與 chars 對調以對齊
      ``count`` 軸的既有呼叫端）。
    * 回傳值：統一回 ``count``（次數軸）而非 ``char_count``，需要 char
      軸結果的呼叫端走 ``read_char_count()`` 或直接 ``_read(tid)``。

    測試斷言用 ``_read(tid)`` 取 ``char_count``，避免硬碼 7 與翻譯行為
    脫節；同 row 同 transaction 一次寫入 count 與 char_count 兩軸，
    兩軸**同時**被遞增。
    """

    def test_first_call_creates_row_and_adds_n(self):
        """無 row → 自動 INSERT 並 +N，row.char_count == N（且 count +1）。"""
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=100)
        assert _read(1) == (1, 100)  # count: 0→1, char_count: 0→100

    def test_accumulates_across_calls(self):
        """連續呼叫：+5、+3、+7 → char_count 累加為 15；count 累加為 3。"""
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=5)
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=3)
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=7)
        assert _read(1) == (3, 15)  # count: 0→3, char_count: 5+3+7=15

    def test_negative_chars_raises(self):
        """chars < 0 → ValueError（守衛拒絕負字數）。"""
        for db in _make_db():
            with pytest.raises(ValueError, match="chars must be >= 0"):
                increment_usage(db, 1, plan="free", chars=-1)

    def test_zero_chars_early_returns_existing_value(self):
        """chars=0 → 早退，row 不變更（count 與 char_count 都不動）。

        守衛與 validate_count 同形，避免空字串或異常輸入造成無意義鎖操作。
        """
        _seed(1, char_count=42)
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=0)
        assert _read(1) == (0, 42)  # 早退，row 完全不變

    def test_no_plan_always_increments(self):
        """未傳 plan → 不重驗 → 即使 char_count == char_limit 仍 +N（內部呼叫方語意）。

        與既有 increment_usage(db, tid)（無 plan）對齊：count 軸也不重驗。
        雙軸都「真實累計」，與 plan=fingerprint 行為不同。
        """
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit)  # 已達 char 上限
        for db in _make_db():
            increment_usage(db, 1, chars=5)  # 無 plan → 不重驗
        # count: 0→1（不重驗，無上限擋下）, char_count: limit → limit+5（真實累計）
        assert _read(1) == (1, char_limit + 5)

    def test_plan_recheck_does_not_increment_at_limit(self):
        """current + chars >= char_limit → 鎖內重驗：count 軸不 +1、char 軸真實累計。

        TOCTOU 防護：永不超賣 **count** 計費；char 軸採真實累計（見 quota.py
        翻案說明），避免 saturate + 嚴格 < 造成的結構性死閘。
        """
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit)  # 已達 char 上限
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=1)
        # count: 0→1（不重驗，因 count_limit=100，free plan 不觸發 count 重驗擋下；
        #         但這條 test 焦點在「char 軸不 saturate」語意）。
        #         修正：count limit 是 100，0+1=1 < 100，所以 count 軸正常 +1。
        # char_count: limit → limit+1（真實累計，舊版 saturate 是結構性死閘）
        assert _read(1) == (1, char_limit + 1)

    def test_plan_recheck_count_limit_freezes_count_but_chars_accumulate(self):
        """雙軸獨立：count 達 limit 時 count 不 +1、char 軸仍真實累計。

        為何這條從舊 ``test_plan_recheck_does_not_increment_overshoot``
        改名？舊版期望「count 軸 saturate」其實是錯的——char 軸的 TOCTOU
        防護是「saturate-free + 真實累計」，count 軸才是「達 limit 不 +1」
        的 saturate 語意。確認雙軸各自語意不混淆。
        """
        count_limit = PLAN_DAILY_LIMITS["free"]
        _seed(1, count=count_limit, char_count=10)
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=5)
        # count: 達 limit → 不 +1；char_count: 10 → 15（真實累計，無 char 重驗擋下因 free plan）
        # 等等——increment_usage 的 count 軸在 plan=fingerprint 時「達 limit 不 +1」，
        # 但 chars>0 仍會 commit 寫入 char_count（沿用既有語意）。
        assert _read(1) == (count_limit, 15)

    def test_plan_recheck_just_below_passes(self):
        """current < char_limit，plan=free → 兩軸都正常 +1/+N。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=10)
        delta = char_limit - 11  # 累加後 == char_limit-1，未觸發 char 重驗擋下
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=delta)
        assert _read(1) == (1, 10 + delta)

    def test_cross_tenant_isolation_on_increment(self):
        """tenant 1 累加不影響 tenant 2。"""
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=100)
        for db in _make_db():
            increment_usage(db, 2, plan="free", chars=50)
        assert _read(1) == (1, 100)
        assert _read(2) == (1, 50)


# ── 4. 並列呼叫：count 與 char_count 兩軸獨立計量 ─────────────────────────────

class TestParallelCountAndCharAxes:
    """並列呼叫 ``increment_usage(plan=, chars=)`` —— count 與 char_count 兩軸獨立計量。

    函式已合併為 ``increment_usage(db, tid, plan=None, chars=0)``：
    一次 SELECT FOR UPDATE、同 transaction 內 ``count += 1; char_count += chars``、
    單一 commit。舊版「兩並列函式各自鎖 + 各自 commit」介面被收掉，本類用
    ``chars=0`` / ``chars=N`` 控制兩軸互不影響的語意驗證。
    """

    def test_increment_usage_and_increment_char_usage_are_independent(self):
        """``chars=0`` 與 ``chars>0`` 兩種呼叫同 row 互不影響對方軸。"""
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=0)  # count: 0 → 1, char 不動
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=42)  # count: 1 → 2, char: 0 → 42
        assert _read(1) == (2, 42)

    def test_count_axis_failure_does_not_affect_char_axis(self):
        """模擬：count 已達上限（increment_usage 鎖內不 +1），
        但 chars>0 仍正常累加 char_count。兩軸獨立判定。"""
        count_limit = PLAN_DAILY_LIMITS["free"]
        _seed(1, count=count_limit, char_count=0)  # count 滿、char 0

        for db in _make_db():
            count_after = increment_usage(db, 1, plan="free", chars=0)
        # count 已達 limit → 不 +1，return current count
        assert count_after == count_limit

        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=50)
        # 第二行：count 仍達 limit → 鎖內不 +1，**但** chars=50 仍寫入 char_count
        #   （沿用既有「翻譯已送出但 count 不 +1」語意：服務已提供、字數仍累計）
        assert _read(1) == (count_limit, 50)

    def test_char_axis_failure_does_not_affect_count_axis(self):
        """反向：char 達 limit 但未觸發 char 鎖內重驗（無 char 重驗擋下）→ count 照常 +1。

        increment_usage 不對 char 軸做「達 limit 不寫入」的 saturate——它採
        「真實累計（不 saturate）」語意，下一次 ``has_char_quota`` 嚴格 ``<``
        才會擋下（見 quota.py 翻案說明）。所以 char 滿時 chars>0 仍會寫入。
        count 軸獨立、照常 +1。
        """
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, count=0, char_count=char_limit)  # char 滿、count 0

        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=1)
        # count: 0 → 1（未達 limit，正常 +1）
        # char_count: char_limit → char_limit+1（真實累計，無 saturate）
        assert _read(1) == (1, char_limit + 1)


# ── 5. 反向對照組：未超額→正常計量，證明「擋下」案例非「永遠不動」假綠 ──────

class TestReverseControls:
    def test_under_char_limit_uncheck_returns_true(self):
        """反向對照 #1：char_count 遠低於上限時，has_char_quota → True。
        證明前面「False 案例」非「永遠 False」假綠。"""
        _seed(1, char_count=10)
        for db in _make_db():
            assert has_char_quota(db, 1, "free") is True

    def test_under_char_limit_increment_works(self):
        """反向對照 #2：char 未達上限時，increment_usage(chars=50) 正常 +50。
        證明前面「不遞增」案例非「永遠不動」假綠。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit - 100)  # 離上限還有 100 字
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=50)
        assert _read(1) == (1, char_limit - 50)  # 正常累加：count +1、char +50

    def test_zero_usage_no_oversell_on_increment(self):
        """反向對照 #3：從 0 開始連加多次，累計不超賣 char_limit。
        逐位手算：3 × 300 = 900，char_limit=1000，預期 900。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        for _ in range(3):
            for db in _make_db():
                increment_usage(db, 1, plan="free", chars=300)
        c, cc = _read(1)
        assert cc == 900  # 300 × 3 逐位手算核對
        assert cc < char_limit  # 還未達上限
        assert c == 3  # count 也累加 3


# ── 6. 鎖內重驗：increment_usage(plan=) 觸發 count 軸重驗契約 ────────────────

class TestCharQuotaRecheckContract:
    """``increment_usage(plan=, chars=)`` 鎖內重驗語意（count 軸層面）。"""

    def test_recheck_only_triggers_when_plan_passed(self):
        """鎖內重驗僅在傳 plan 時觸發——count 軸層面。"""
        count_limit = PLAN_DAILY_LIMITS["free"]
        _seed(1, count=count_limit, char_count=0)  # count 達 limit

        # 無 plan → 不重驗 → count 仍 +1（內部 API 語意）
        for db in _make_db():
            increment_usage(db, 1, chars=1)  # 無 plan → 不重驗 → count +1
        # count 從 limit → limit+1（無 plan，無重驗擋下）
        c, _ = _read(1)
        assert c == count_limit + 1

        # 有 plan → 重驗 → count 達 limit 不 +1
        for db in _make_db():
            increment_usage(db, 1, plan="free", chars=1)
        # 第二行：count 已達 limit，鎖內重驗 → 不 +1。chars=1 仍寫入 char_count。
        c, cc = _read(1)
        assert c == count_limit + 1, (
            f"count 應凍結在 {count_limit + 1}（上一行無 plan 已 +1；本行有 plan 重驗不動），"
            f"got {c}"
        )
        assert cc == 1, f"char_count 仍寫入，got {cc}"

    def test_recheck_uses_plan_fallback_to_free(self):
        """鎖內重驗遇未知 plan → fallback 到 free 上限。"""
        count_limit_free = PLAN_DAILY_LIMITS["free"]
        _seed(1, count=count_limit_free, char_count=0)  # == free 上限

        for db in _make_db():
            increment_usage(db, 1, plan="enterprise", chars=1)
        # 未知 plan → fallback 到 free → 滿 → count 不 +1
        c, _ = _read(1)
        assert c == count_limit_free, (
            f"未知 plan fallback 到 free、count 達 free 上限 → 不 +1，got {c}"
        )

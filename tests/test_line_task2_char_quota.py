"""Task #2 QA 驗收測試 — 字數軸（char_count）常數 + 檢查/遞增函式。

驗收焦點（任務 #2）：
  * PLAN_DAILY_CHAR_LIMITS 常數結構正確（free / pro / 未知 plan fallback 對齊既有 free）
  * has_char_quota：read-only、不 commit、帶 needed 預估、未超額 True / 超額 False、
    未知 plan fallback、needed < 0 拒絕
  * increment_char_usage：+N 累加、chars <= 0 拒絕、plan 觸發鎖內重驗
    （永不超賣 char_limit）、與既有 increment_usage 並列呼叫兩軸獨立計量
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
    increment_char_usage,
    increment_usage,
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


# ── 3. increment_char_usage — 副作用成功後才計量 +N ──────────────────────────

class TestIncrementCharUsage:
    def test_first_call_creates_row_and_adds_n(self):
        """無 row → 自動 INSERT 並 +N，row.char_count == N。

        簽名 increment_char_usage(db, tenant_id, chars, plan=None)——
        位置參數依序 tenant_id、chars。"""
        for db in _make_db():
            result = increment_char_usage(db, 1, 100, plan="free")
        assert result == 100
        _, cc = _read(1)
        assert cc == 100

    def test_accumulates_across_calls(self):
        """連續呼叫：+5、+3、+7 → 累加為 15。"""
        for db in _make_db():
            increment_char_usage(db, 1, 5, plan="free")
        for db in _make_db():
            increment_char_usage(db, 1, 3, plan="free")
        for db in _make_db():
            increment_char_usage(db, 1, 7, plan="free")
        _, cc = _read(1)
        assert cc == 15  # 5+3+7 逐位手算核對

    def test_negative_chars_raises(self):
        """chars < 0 → ValueError（守衛拒絕負字數）。"""
        for db in _make_db():
            with pytest.raises(ValueError, match="chars must be >= 0"):
                increment_char_usage(db, 1, -1, plan="free")

    def test_zero_chars_early_returns_existing_value(self):
        """chars=0 → 早退，回傳現值，不遞增、不變更 row。
        守衛與 validate_count 同形，避免空字串或異常輸入造成無意義鎖操作。"""
        _seed(1, char_count=42)
        for db in _make_db():
            result = increment_char_usage(db, 1, 0, plan="free")
        assert result == 42  # 早退回傳現值
        _, cc = _read(1)
        assert cc == 42  # 不變更

    def test_no_plan_always_increments(self):
        """未傳 plan → 不重驗 → 即使 char_count == char_limit 仍 +1（內部呼叫方語意）。

        與既有 increment_usage(db, tid)（無 plan）對齊。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit)  # 已達上限
        for db in _make_db():
            result = increment_char_usage(db, 1, 5)  # 無 plan → 不重驗
        assert result == char_limit + 5  # 超賣（內部呼叫語意，外部應走 plan 觸發重驗）
        _, cc = _read(1)
        assert cc == char_limit + 5

    def test_plan_recheck_does_not_increment_at_limit(self):
        """current + chars >= char_limit → 鎖內重驗不遞增、回傳現值。
        TOCTOU 防護：永不超賣計費。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit)  # 已達上限
        for db in _make_db():
            result = increment_char_usage(db, 1, 1, plan="free")
        assert result == char_limit  # 未遞增
        _, cc = _read(1)
        assert cc == char_limit  # DB 仍 == char_limit，永不超賣

    def test_plan_recheck_does_not_increment_overshoot(self):
        """current=10, chars=char_limit → 10+limit >= limit → 不遞增。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=10)
        for db in _make_db():
            result = increment_char_usage(db, 1, char_limit, plan="free")
        assert result == 10
        _, cc = _read(1)
        assert cc == 10

    def test_plan_recheck_just_below_passes(self):
        """current=10, chars=char_limit-11, plan=free → 10+(limit-11)=limit-1 < limit → 遞增。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=10)
        delta = char_limit - 11
        for db in _make_db():
            result = increment_char_usage(db, 1, delta, plan="free")
        assert result == 10 + delta
        _, cc = _read(1)
        assert cc == 10 + delta

    def test_cross_tenant_isolation_on_increment(self):
        """tenant 1 累加不影響 tenant 2。"""
        for db in _make_db():
            increment_char_usage(db, 1, 100, plan="free")
        for db in _make_db():
            increment_char_usage(db, 2, 50, plan="free")
        _, c1 = _read(1)
        _, c2 = _read(2)
        assert c1 == 100
        assert c2 == 50


# ── 4. 並列呼叫：count 與 char_count 兩軸獨立計量 ─────────────────────────────

class TestParallelCountAndCharAxes:
    def test_increment_usage_and_increment_char_usage_are_independent(self):
        """兩函式各自 +1 與 +N，同一 row 的 count 與 char_count 互不影響。"""
        for db in _make_db():
            increment_usage(db, 1, plan="free")              # count: 0 → 1
        for db in _make_db():
            increment_char_usage(db, 1, 42, plan="free")     # char_count: 0 → 42
        c, cc = _read(1)
        assert c == 1
        assert cc == 42

    def test_count_axis_failure_does_not_affect_char_axis(self):
        """模擬：count 已達上限（increment_usage 鎖內不遞增），
        但字數軸仍可正常遞增。兩軸獨立判定。"""
        count_limit = PLAN_DAILY_LIMITS["free"]
        _seed(1, count=count_limit, char_count=0)  # count 滿、char 0

        for db in _make_db():
            count_after = increment_usage(db, 1, plan="free")
        assert count_after == count_limit  # 不遞增

        for db in _make_db():
            char_after = increment_char_usage(db, 1, 50, plan="free")
        assert char_after == 50  # 字數軸照常 +50

        c, cc = _read(1)
        assert c == count_limit  # count 軸凍結
        assert cc == 50          # char 軸獨立

    def test_char_axis_failure_does_not_affect_count_axis(self):
        """反向：char 滿、count 可正常 +1。兩閘獨立擋下。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, count=0, char_count=char_limit)  # char 滿、count 0

        for db in _make_db():
            char_after = increment_char_usage(db, 1, 1, plan="free")
        assert char_after == char_limit  # 字數軸不遞增

        for db in _make_db():
            count_after = increment_usage(db, 1, plan="free")
        assert count_after == 1  # count 軸照常 +1

        c, cc = _read(1)
        assert c == 1            # count 軸獨立
        assert cc == char_limit  # char 軸凍結


# ── 5. 反向對照組：未超額→正常計量，證明「擋下」案例非「永遠不動」假綠 ──────

class TestReverseControls:
    def test_under_char_limit_uncheck_returns_true(self):
        """反向對照 #1：char_count 遠低於上限時，has_char_quota → True。
        證明前面「False 案例」非「永遠 False」假綠。"""
        _seed(1, char_count=10)
        for db in _make_db():
            assert has_char_quota(db, 1, "free") is True

    def test_under_char_limit_increment_works(self):
        """反向對照 #2：char 未達上限時，increment_char_usage 正常 +N。
        證明前面「不遞增」案例非「永遠不動」假綠。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit - 100)  # 離上限還有 100 字
        for db in _make_db():
            result = increment_char_usage(db, 1, 50, plan="free")
        assert result == char_limit - 50
        _, cc = _read(1)
        assert cc == char_limit - 50  # 正常累加

    def test_zero_usage_no_oversell_on_increment(self):
        """反向對照 #3：從 0 開始連加多次，累計不超賣 char_limit。
        逐位手算：3 × 300 = 900，char_limit=1000，預期 900。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        for _ in range(3):
            for db in _make_db():
                increment_char_usage(db, 1, 300, plan="free")
        _, cc = _read(1)
        assert cc == 900  # 300 × 3 逐位手算核對
        assert cc < char_limit  # 還未達上限


# ── 6. 鎖內重驗：increment_char_usage 與既有 increment_usage 採同形 contract ──

class TestCharQuotaRecheckContract:
    def test_recheck_only_triggers_when_plan_passed(self):
        """鎖內重驗僅在傳 plan 時觸發——與既有 increment_usage 對齊。"""
        char_limit = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit)  # 滿

        # 無 plan → 不重驗 → 仍遞增（內部 API 語意）
        for db in _make_db():
            no_plan = increment_char_usage(db, 1, 1)
        assert no_plan == char_limit + 1

        # 有 plan → 重驗 → 不遞增
        for db in _make_db():
            with_plan = increment_char_usage(db, 1, 1, plan="free")
        assert with_plan == char_limit + 1  # 上一行已 commit 到 +1；此次重驗不動
        # 最終值 == char_limit + 1（無 plan 觸發 +1、有 plan 觸發不動）
        _, cc = _read(1)
        assert cc == char_limit + 1

    def test_recheck_uses_plan_fallback_to_free(self):
        """鎖內重驗遇未知 plan → fallback 到 free 上限。
        與常數 fallback 語意一致。"""
        char_limit_free = PLAN_DAILY_CHAR_LIMITS["free"]
        _seed(1, char_count=char_limit_free)  # == free 上限

        for db in _make_db():
            result = increment_char_usage(db, 1, 1, plan="enterprise")
        # 未知 plan → fallback 到 free → 滿 → 不遞增
        assert result == char_limit_free
        _, cc = _read(1)
        assert cc == char_limit_free

"""Plan / quota 定義與計量邏輯。

配額規則
--------
* free : 每日 100 次 API 呼叫、1 000 字翻譯
* pro  : 每日 10 000 次 API 呼叫、100 000 字翻譯

超量時拋出 HTTP 429 並附說明訊息。

字數軸（char_count）與次數軸（count）獨立計量、獨立超額擋下：
* has_quota / has_char_quota 任一不通 → LINE webhook 不翻譯、回配額訊息
* increment_usage / increment_char_usage 並列呼叫、各自 SELECT FOR UPDATE
* 兩軸均採「後扣」語意：副作用成功後才計量，下游失敗拋出時不白扣
* 「單次溢出可接受」：has_* 與 increment_* 之間存在 TOCTOU 窗口，鎖內
  重驗確保永不超賣計費（恢復舊版 check_and_increment 的原子保證）
"""

from __future__ import annotations

import datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.auth.dependencies import Actor, get_current_actor
from saas_mvp.db import get_db
from saas_mvp.models.usage import ApiUsage
from saas_mvp.models.user import User

# SQLite 不支援 SKIP LOCKED，但 with_for_update() 在 SQLite 會升為
# connection-level exclusive lock，足以序列化同一程序內的並發寫入。
# PostgreSQL 則完整使用 SELECT … FOR UPDATE 行鎖。

# ── 配額常數 ─────────────────────────────────────────────────
PLAN_DAILY_LIMITS: dict[str, int] = {
    "free": 100,
    "pro": 10_000,
}

# 字數上限（每日）。與次數軸同形 dict：plan → 上限字元數。
# PM 議程拍板：free=1000、pro=100000。值若需調整改本常數即可，呼叫端
# 一律透過 PLAN_DAILY_CHAR_LIMITS.get(plan, ...) 取用，無硬碼。
PLAN_DAILY_CHAR_LIMITS: dict[str, int] = {
    "free": 1_000,
    "pro": 100_000,
}

_429 = HTTPException(
    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    detail="Quota exceeded for today. Upgrade to pro for higher limits.",
)


def validate_count(value: object) -> int:
    """確認 value 是合法非負整數（排除 bool 混入）。"""
    if isinstance(value, bool):
        raise TypeError(f"Count must be int, got bool: {value!r}")
    if not isinstance(value, int):
        raise TypeError(f"Count must be int, got {type(value).__name__}: {value!r}")
    if value < 0:
        raise ValueError(f"Count must be >= 0, got {value}")
    return value


def _get_or_create_usage_locked(db: Session, tenant_id: int, today: datetime.date) -> ApiUsage:
    """取得今日計量列（帶 FOR UPDATE 鎖定），不存在則先 INSERT count=0。"""
    row = db.execute(
        select(ApiUsage)
        .where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if row is None:
        try:
            row = ApiUsage(tenant_id=tenant_id, period=today, count=0)
            db.add(row)
            db.flush()
        except Exception:
            db.rollback()
            row = db.execute(
                select(ApiUsage)
                .where(
                    ApiUsage.tenant_id == tenant_id,
                    ApiUsage.period == today,
                )
                .with_for_update()
            ).scalar_one()

    return row


def check_and_increment(db: Session, tenant_id: int, plan: str) -> int:
    """檢查今日配額；未超量則 +1 並 commit；超量拋 HTTP 429。

    使用 SELECT FOR UPDATE 序列化並發存取，消除 read-check-write 競態。
    回傳更新後的 count。
    """
    limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"])
    today = datetime.date.today()

    row = _get_or_create_usage_locked(db, tenant_id, today)

    validate_count(row.count)   # 防衛性：DB 異常值（含 bool）一律攔截

    if row.count >= limit:
        raise _429

    row.count += 1
    db.commit()
    return row.count


def has_quota(db: Session, tenant_id: int, plan: str) -> bool:
    """非遞增檢查：今日是否仍有配額（不寫入、不 commit）。

    用於「副作用成功後才計量」流程：先以本函式判斷是否放行，
    待下游副作用（翻譯、回覆）皆成功後，再呼叫 :func:`increment_usage`。
    回傳 True 代表仍有額度，False 代表已達上限。
    """
    limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"])
    today = datetime.date.today()
    row = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
    ).scalar_one_or_none()
    used = row.count if row else 0
    validate_count(used)   # 防衛性：DB 異常值（含 bool）一律攔截
    return used < limit


def increment_usage(db: Session, tenant_id: int, plan: str | None = None) -> int:
    """副作用成功後才計量 +1（SELECT FOR UPDATE 序列化）並 commit。

    與 :func:`has_quota` 搭配使用：呼叫端應先以 has_quota 判斷是否放行，
    待下游副作用全部成功後才呼叫本函式，避免下游失敗造成「白扣」。

    若提供 ``plan``，會在鎖內**重驗** ``count < limit``：has_quota 與
    increment_usage 之間存在 TOCTOU 窗口，並發請求可能都通過 has_quota；
    鎖內重驗確保 count 永不超過 limit（恢復舊版 check_and_increment 的
    原子保證）。已達上限時不遞增、回傳現值（極端並發下偶有「免費一次」，
    但永不超賣計費，較超量計費更安全）。

    回傳更新後的 count。
    """
    today = datetime.date.today()
    row = _get_or_create_usage_locked(db, tenant_id, today)
    validate_count(row.count)

    if plan is not None:
        limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"])
        if row.count >= limit:
            # TOCTOU：has_quota 放行後、本函式取得鎖前，配額已被並發請求用盡。
            db.commit()  # 釋放 FOR UPDATE 鎖，不遞增
            return row.count

    row.count += 1
    db.commit()
    return row.count


def _get_or_create_key_usage_locked(
    db: Session, api_key_id: int, tenant_id: int, today: datetime.date
):
    """取得今日 per-key 計量列（帶 FOR UPDATE 鎖定）。"""
    from saas_mvp.models.api_key_usage import ApiKeyUsage  # 避免頂層循環 import

    row = db.execute(
        select(ApiKeyUsage)
        .where(
            ApiKeyUsage.api_key_id == api_key_id,
            ApiKeyUsage.period == today,
        )
        .with_for_update()
    ).scalar_one_or_none()

    if row is None:
        try:
            row = ApiKeyUsage(api_key_id=api_key_id, tenant_id=tenant_id,
                              period=today, count=0)
            db.add(row)
            db.flush()
        except Exception:
            db.rollback()
            row = db.execute(
                select(ApiKeyUsage)
                .where(
                    ApiKeyUsage.api_key_id == api_key_id,
                    ApiKeyUsage.period == today,
                )
                .with_for_update()
            ).scalar_one()

    return row


def check_and_increment_key(db: Session, api_key_id: int, tenant_id: int) -> int:
    """原子遞增 per-key 計量；DB 例外向上傳播（絕不靜默吞掉）。"""
    today = datetime.date.today()
    row = _get_or_create_key_usage_locked(db, api_key_id, tenant_id, today)
    row.count += 1
    db.commit()
    return row.count


def get_quota_status(db: Session, tenant_id: int, plan: str) -> dict:
    """回傳今日用量狀態（供 API 查詢）。

    回傳欄位：
      plan             : 目前方案
      period           : 計量日期（ISO 8601，UTC）
      used             : 今日已用 API 呼叫次數
      limit            : 每日 API 呼叫次數上限
      remaining        : 剩餘 API 呼叫次數（max 0）
      used_chars       : 今日已翻譯字元數
      char_limit       : 每日翻譯字元上限
      remaining_chars  : 剩餘可翻譯字元數（max 0）
    """
    today = datetime.date.today()
    limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"])
    char_limit = PLAN_DAILY_CHAR_LIMITS.get(plan, PLAN_DAILY_CHAR_LIMITS["free"])
    row = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
    ).scalar_one_or_none()
    # 既有 NULL 列由讀取端兜底 0（model default=0 僅對新 INSERT 生效；
    # 既有 row 的 char_count 為 NULL，需在此避免 ArithmeticError / None 比較）。
    used = row.count if row else 0
    used_chars = (row.char_count or 0) if row else 0
    return {
        "plan": plan,
        "period": today.isoformat(),
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "used_chars": used_chars,
        "char_limit": char_limit,
        "remaining_chars": max(0, char_limit - used_chars),
    }


# ── 字數軸：與次數軸同形並列 ────────────────────────────────────────────────

def has_char_quota(db: Session, tenant_id: int, plan: str, needed: int = 0) -> bool:
    """非遞增檢查：今日是否仍有字數配額（不寫入、不 commit）。

    與 :func:`has_quota` 同形並列，獨立查詢、獨立擋下。呼叫端可傳入
    ``needed`` 表示本次翻譯預估字數；若 ``row.char_count + needed >= limit``
    視為超額，False。

    回傳 True 代表仍有字數額度，False 代表已達上限。
    """
    char_limit = PLAN_DAILY_CHAR_LIMITS.get(plan, PLAN_DAILY_CHAR_LIMITS["free"])
    today = datetime.date.today()
    row = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
    ).scalar_one_or_none()
    # 讀取端兜底 0：既有 NULL 列在 (or 0) 後安全可比
    used_chars = (row.char_count or 0) if row else 0
    validate_count(used_chars)
    if needed < 0:
        raise ValueError(f"needed must be >= 0, got {needed}")
    return (used_chars + needed) < char_limit


def increment_char_usage(
    db: Session, tenant_id: int, chars: int, plan: str | None = None
) -> int:
    """副作用成功後才計量 +N 字（SELECT FOR UPDATE 序列化）並 commit。

    與 :func:`increment_usage` 同形並列：介面對稱、各自一次鎖，不合併。
    呼叫端應先以 has_char_quota 判斷是否放行，待下游副作用全部成功後才呼叫
    本函式，避免下游失敗造成「白扣字數」。

    若提供 ``plan``，會在鎖內**重驗** ``char_count + chars < char_limit``：
    has_char_quota 與 increment_char_usage 之間存在 TOCTOU 窗口，並發請求
    可能都通過 has_char_quota；鎖內重驗確保 char_count 永不超過 char_limit
    （永不超賣計費）。已達上限時不遞增、回傳現值。

    回傳更新後的 char_count。
    """
    if chars < 0:
        raise ValueError(f"chars must be >= 0, got {chars}")
    today = datetime.date.today()
    row = _get_or_create_usage_locked(db, tenant_id, today)
    # 既有 NULL 列兜底 0
    current = row.char_count or 0
    validate_count(current)

    if plan is not None:
        char_limit = PLAN_DAILY_CHAR_LIMITS.get(plan, PLAN_DAILY_CHAR_LIMITS["free"])
        if current + chars >= char_limit:
            # TOCTOU：has_char_quota 放行後、本函式取得鎖前，配額已被並發請求用盡。
            db.commit()  # 釋放 FOR UPDATE 鎖，不遞增
            return current

    row.char_count = current + chars
    db.commit()
    return row.char_count


# ── FastAPI dependency ────────────────────────────────────────

def require_quota(
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> User:
    """dependency：驗證 tenant quota 並計量 +1；若以 API key 認證則同時累計 per-key 計數。

    用法（router 層）::

        @router.post("/notes", dependencies=[Depends(require_quota)])
        def create_note(...): ...

    技術債（M2 移交，兩種計費語意並存，勿誤判為 bug）：

    ① 前扣語意：本 dependency 採 ``check_and_increment``——請求一進來就原子
       遞增計量，**先扣再執行業務**。一般 API（Notes/Billing 等）無明確的
       「成功副作用」定義，故維持前扣；若業務本身失敗，該次仍已計量。

    ② 後扣語意：LINE webhook 路徑（``line_webhook.py`` 6a→6d）改採
       ``has_quota`` → 翻譯/回覆 → ``increment_usage``，**副作用成功後才計量**，
       消除下游失敗造成的白扣。但其代價是「單次溢出」：has_quota 放行後，
       若 increment_usage 鎖內重驗發現配額已被並發請求用盡，該次翻譯/回覆
       **已送出但不計量**——此為設計可接受的一次性溢出（永不超賣計費），
       非 bug。

    M2 若要將一般 API 統一為後扣，須：(a) 為各端點定義明確的「成功副作用」
    邊界；(b) 重新評估上述單次溢出是否仍可接受、以及兩套路徑的整合。
    在此之前，兩種語意刻意並存。
    """
    # 1. tenant-level 配額檢查（超量拋 429）
    check_and_increment(db, actor.user.tenant_id, actor.user.tenant.plan)
    # 2. per-key 計量（僅 API key 認證時，DB 例外一律傳播回 500）
    if actor.api_key_id is not None:
        check_and_increment_key(db, actor.api_key_id, actor.user.tenant_id)
    return actor.user

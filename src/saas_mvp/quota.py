"""Plan / quota 定義與計量邏輯。

配額規則
--------
* free : 每日 100 次 API 呼叫、1 000 字翻譯
* pro  : 每日 10 000 次 API 呼叫、100 000 字翻譯

超量時拋出 HTTP 429 並附說明訊息。

字數軸（char_count）與次數軸（count）獨立計量、獨立超額擋下：
* has_quota / has_char_quota 任一不通 → LINE webhook 不翻譯、回配額訊息
* 兩軸採**單一** increment_usage(plan, chars=0)：同 row 一次 SELECT FOR UPDATE、
  同 transaction 內 ``count += 1; char_count += chars``、單一 commit
  —— 翻案自舊版「兩並列 increment 函式」：少一輪 DB 往返 + 少一次 commit，
  鎖窗口由兩次壓成一次，TOCTOU 與 commit 失敗率同向下降
* 兩軸均採「後扣」語意：副作用成功後才計量，下游失敗拋出時不白扣
* 「單次溢出可接受」：has_* 與 increment_usage 之間存在 TOCTOU 窗口，鎖內
  重驗確保兩軸**不 saturate**（真實累計接受單次超賣）——重點是「下次
  has_char_quota 真的擋下」，而非「單次永不超賣」
* 翻案理由（異議檢查者實跑出的結構性死閘）：舊版 saturate（停在原值）
  + has_char_quota(needed=0) 嚴格 ``<`` → char_count 永遠 < limit，
  閘永遠放行。真實累計讓 char_count 能達/超 limit，閘才在後續擋下。
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
    "standard": 2_000,
    "pro": 10_000,
}

# 字數上限（每日）。與次數軸同形 dict：plan → 上限字元數。
# PM 議程拍板：free=1000、pro=100000；B1 新增 standard=20000。值若需調整改
# 本常數即可，呼叫端一律透過 PLAN_DAILY_CHAR_LIMITS.get(plan, ...) 取用，無硬碼。
PLAN_DAILY_CHAR_LIMITS: dict[str, int] = {
    "free": 1_000,
    "standard": 20_000,
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
    """非遞增檢查：今日是否仍有次數配額（不寫入、不 commit）。

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


def has_char_quota(db: Session, tenant_id: int, plan: str) -> bool:
    """非遞增檢查：今日是否仍有字數配額（不寫入、不 commit）。

    與 :func:`has_quota` 同形並列、獨立查詢。閘語意採嚴格 ``<``——
    一旦 char_count 達到/超過 char_limit，後續請求一律擋下。
    配合「真實累計（不 saturate）」的 increment_usage，下一次呼叫即可
    正確觀察到閘擋下，解決舊版「saturate + 嚴格 <」造成的結構性死閘。

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
    # 讀取端兜底 0：DDL 帶 server_default 保證新 row 永非 NULL；
    # ``(or 0)`` 為 defense in depth，吸收極罕見的相容性 row。
    used_chars = (row.char_count or 0) if row else 0
    validate_count(used_chars)
    return used_chars < char_limit


def increment_usage(
    db: Session, tenant_id: int, plan: str | None = None, chars: int = 0
) -> int:
    """單一鎖 + 單一 commit：次數 +1、字數 +chars，兩軸獨立重驗 limit。

    與 :func:`has_quota` / :func:`has_char_quota` 搭配使用：呼叫端應先
    兩道閘皆判斷通過，待下游副作用全部成功後才呼叫本函式，避免下游失敗
    造成「白扣」。

    重驗語意（**不 saturate / 接受單次超賣**）：
      * count 達 limit → 不 +1（沿用舊版邏輯，極罕見 TOCTOU）
      * char_count 達/超 char_limit → 仍**真實累計** `current + chars`
        寫入 char_count（不 saturate、停在原值）。理由：saturate 會造成
        結構性死閘（char_count 永遠 < limit → has_char_quota 永不擋下），
        真實累計讓 char_count 能達/超 limit，下一次 has_char_quota 真的擋下。
        代價是「單次溢出」——同次數軸的「單次溢出可接受」語意對齊，
        永不超賣計費的舊期待被**明確捨棄**。

    已知失敗模式（既有，沿用）：「翻譯/回覆成功但本函式 commit 失敗 = 已服務
    未計費」。修法需先定義「成功副作用邊界」，跨 webhook 設計面，本輪不修、
    留 issue tracker。

    早退：`chars <= 0` 時不開鎖、不 commit，直接讀 row 後回傳 count。
    ``chars < 0`` 為程式錯誤（validate_count 守衛）。

    回傳更新後的 count（向後相容既有測試契約；需要 char_count 的呼叫端
    再以 `select(ApiUsage)` 自取，或改用 `read_char_count()`）。
    """
    if chars < 0:
        raise ValueError(f"chars must be >= 0, got {chars}")
    today = datetime.date.today()
    row = _get_or_create_usage_locked(db, tenant_id, today)
    current_count = row.count
    current_chars = row.char_count or 0   # defense in depth
    validate_count(current_count)
    validate_count(current_chars)

    # ── count 軸重驗（沿用舊語意：達 limit 不 +1，避免超賣計費） ──────────
    count_limit = PLAN_DAILY_LIMITS.get(plan, PLAN_DAILY_LIMITS["free"]) if plan else None
    if count_limit is not None and current_count >= count_limit:
        # TOCTOU：has_quota 放行後、本函式取得鎖前，配額已被並發請求用盡。
        # chars 軸仍可遞增（call 雖被閘擋下但翻譯已送出，是更接近的現實）
        # —— 但若整體已超賣，回退到「不寫入、釋放鎖」最簡單保守。
        if chars > 0:
            # 仍寫入 char_count（真實累計），但不 +1 count
            row.char_count = current_chars + chars
        db.commit()
        return row.count

    # ── chars 軸：真實累計，不 saturate（翻案重點） ─────────────────────────
    # 即使 current_chars + chars 超 char_limit，仍寫入真實值。
    # 下次 has_char_quota 在「達/超 limit」時正確擋下，閘真實有效。
    new_count = current_count + 1
    if chars > 0:
        row.count = new_count
        row.char_count = current_chars + chars
    else:
        row.count = new_count
    db.commit()
    return row.count


def read_char_count(db: Session, tenant_id: int) -> int:
    """讀取今日 char_count（無 row 時回 0）。

    :func:`increment_usage` 為向後相容僅回傳 count；需要 char_count 的
    呼叫端（測試、debug endpoint）走本函式。
    """
    today = datetime.date.today()
    row = db.execute(
        select(ApiUsage).where(
            ApiUsage.tenant_id == tenant_id,
            ApiUsage.period == today,
        )
    ).scalar_one_or_none()
    return (row.char_count or 0) if row else 0


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
    # 讀取端兜底 0：DDL 帶 server_default 保證新 row 永非 NULL；
    # ``(or 0)`` 為 defense in depth，吸收極罕見的相容性 row
    # （例如測試直接用 session.add() 繞過 column default），
    # 避免 ArithmeticError / None 比較。
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
    # 1. tenant-level 配額檢查（超量拋 429）；用 effective_plan 讓試用中租戶
    #    享試用方案的配額（延遲 import 避免 quota ↔ services 循環）。
    from saas_mvp.services.plans import effective_plan

    check_and_increment(db, actor.user.tenant_id, effective_plan(actor.user.tenant))
    # 2. per-key 計量（僅 API key 認證時，DB 例外一律傳播回 500）
    if actor.api_key_id is not None:
        check_and_increment_key(db, actor.api_key_id, actor.user.tenant_id)
    return actor.user

"""行銷自動化服務 — 受眾挑選 + 活動執行（claim + 獎勵 + 推播）。

eligible_customers：依 campaign.type 與 segment_json 算出符合的顧客（複用
services/segments.segment_customers）。

run_campaign：逐顧客（上限 cap 內）—
  1. INSERT 一筆 CampaignSend claim（catch IntegrityError 跳過重複；UniqueConstraint
     (campaign_id, customer_id, period_key) 同時做冪等與上限）—— 比照
     reminders.enqueue 的去重手法。
  2. 派發獎勵（coupon → services/coupons.redeem_coupon；points → membership.earn_points）。
  3. 透過 LinePushClient 推播訊息。
  4. 標 sent / failed。
  每位顧客各自 try/except，單一失敗不中斷整批（per-customer isolation）。

create_welcome_send：新顧客建檔時的內嵌觸發（period_key='once'，不同步發送，
只入列一筆 pending CampaignSend，由 cron/手動 run_campaign 發送）。
"""

from __future__ import annotations

import datetime
import json

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.line_client import LinePushClient
from saas_mvp.models.campaign import (
    CAMPAIGN_BIRTHDAY,
    CAMPAIGN_REACTIVATION,
    CAMPAIGN_WELCOME,
    REWARD_COUPON,
    REWARD_POINTS,
    Campaign,
)
from saas_mvp.models.campaign_send import (
    CAMPAIGN_SEND_FAILED,
    CAMPAIGN_SEND_PENDING,
    CAMPAIGN_SEND_SENT,
    CampaignSend,
)
from saas_mvp.models.coupon import Coupon
from saas_mvp.models.customer import Customer
from saas_mvp.models.campaign_send import CAMPAIGN_SEND_SKIPPED
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import push_quota as push_quota_svc
from saas_mvp.services import segments as segments_svc
from saas_mvp.services.tenants import tenant_query


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _naive(dt: datetime.datetime | None) -> datetime.datetime | None:
    """SQLite 讀回為 naive；比較前統一去 tzinfo 避免 aware/naive 混比。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def validate_schedule_window(
    schedule_at: datetime.datetime | None,
    expires_at: datetime.datetime | None,
) -> None:
    """排程視窗顛倒的活動永遠不會（或立即過期）發送——直接擋。"""
    s, e = _naive(schedule_at), _naive(expires_at)
    if s is not None and e is not None and e <= s:
        raise HTTPException(
            status_code=422, detail="expires_at must be after schedule_at"
        )


def _campaign_or_404(db: Session, tenant_id: int, campaign_id: int) -> Campaign:
    campaign = (
        tenant_query(db, Campaign, tenant_id)
        .filter(Campaign.id == campaign_id)
        .first()
    )
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found"
        )
    return campaign


def update_campaign(
    db: Session,
    *,
    tenant_id: int,
    campaign_id: int,
    name: str | None = None,
    message_template: str | None = None,
    schedule_at: datetime.datetime | None = None,
    expires_at: datetime.datetime | None = None,
    segment_json: str | None = None,
    reward_type: str | None = None,
    reward_value: int | None = None,
    is_active: bool | None = None,
) -> Campaign:
    """更新活動欄位（僅帶入者覆寫）；排程視窗以合併後的值驗證。"""
    campaign = _campaign_or_404(db, tenant_id, campaign_id)
    validate_schedule_window(
        schedule_at if schedule_at is not None else campaign.schedule_at,
        expires_at if expires_at is not None else campaign.expires_at,
    )
    if name is not None:
        campaign.name = name
    if message_template is not None:
        campaign.message_template = message_template
    if schedule_at is not None:
        campaign.schedule_at = schedule_at
    if expires_at is not None:
        campaign.expires_at = expires_at
    if segment_json is not None:
        campaign.segment_json = segment_json
    if reward_type is not None:
        campaign.reward_type = reward_type
    if reward_value is not None:
        campaign.reward_value = reward_value
    if is_active is not None:
        campaign.is_active = is_active
    db.commit()
    db.refresh(campaign)
    return campaign


def deactivate_campaign(db: Session, *, tenant_id: int, campaign_id: int) -> None:
    """軟刪：停用活動（REST DELETE 的既有語意；排程器不再撿取）。"""
    campaign = _campaign_or_404(db, tenant_id, campaign_id)
    campaign.is_active = False
    db.commit()


def delete_campaign(db: Session, *, tenant_id: int, campaign_id: int) -> None:
    """刪除活動；已有發送紀錄者擋下（請改用停用，保留發送軌跡）。"""
    campaign = _campaign_or_404(db, tenant_id, campaign_id)
    sent = (
        tenant_query(db, CampaignSend, tenant_id)
        .filter(CampaignSend.campaign_id == campaign_id)
        .first()
    )
    if sent is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="此活動已有發送紀錄，請改用停用",
        )
    db.delete(campaign)
    db.commit()


def _parse_segment(campaign: Campaign) -> dict:
    """解析 campaign.segment_json（容錯：非法/空回 {}）。"""
    raw = campaign.segment_json
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _coerce_int(value: object) -> int | None:
    """容錯整數轉換：非法（非數字字串等）回 None（視為該條件不存在）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _segment_kwargs(filters: dict) -> dict:
    """把 segment_json 的鍵正規化為 segment_customers 可接受的 kwargs。

    int 轉換一律容錯（malformed 視為缺漏），避免使用者提供的 segment_json
    讓 /campaigns/{id}/run 因 int('abc') 噴 500。
    """
    out: dict = {}
    if filters.get("tag_ids"):
        out["tag_ids"] = list(filters["tag_ids"])
    if filters.get("tier"):
        out["tier"] = filters["tier"]
    if filters.get("min_bookings") is not None:
        mb = _coerce_int(filters["min_bookings"])
        if mb is not None:
            out["min_bookings"] = mb
    if filters.get("location_id") is not None:
        loc = _coerce_int(filters["location_id"])
        if loc is not None:
            out["location_id"] = loc
    return out


def period_key_for(campaign: Campaign, now: datetime.datetime) -> str:
    """依 campaign.type 算出本次 send 的 period_key（冪等視窗）。"""
    ctype = campaign.type
    if ctype == CAMPAIGN_WELCOME:
        return "once"
    if ctype == CAMPAIGN_REACTIVATION:
        return now.strftime("%Y%m%d")
    if ctype in (CAMPAIGN_BIRTHDAY, "spend"):
        return now.strftime("%Y")
    # broadcast / 其他：每活動一次。
    return f"camp{campaign.id}"


def eligible_customers(
    db: Session, campaign: Campaign, now: datetime.datetime
) -> list[Customer]:
    """依 campaign.type 與 segment_json 算出符合的顧客清單。"""
    filters = _parse_segment(campaign)
    kwargs = _segment_kwargs(filters)

    if campaign.type == CAMPAIGN_BIRTHDAY:
        # 生日活動：當天（月/日）生日，再套 segment_json 其他條件。
        candidates = segments_svc.segment_customers(
            db, tenant_id=campaign.tenant_id, **kwargs
        )
        return [
            c
            for c in candidates
            if c.birthday is not None
            and c.birthday.month == now.month
            and c.birthday.day == now.day
        ]

    if campaign.type == CAMPAIGN_REACTIVATION:
        cutoff = now - datetime.timedelta(days=settings.reactivation_dormant_days)
        return segments_svc.segment_customers(
            db,
            tenant_id=campaign.tenant_id,
            last_booked_before=cutoff,
            **kwargs,
        )

    # spend / welcome / broadcast：純 segment_json 篩選。
    return segments_svc.segment_customers(
        db, tenant_id=campaign.tenant_id, **kwargs
    )


def _render(template: str, customer: Customer) -> str:
    """簡易訊息渲染：替換 {name} 為顧客顯示名。"""
    name = customer.display_name or "顧客"
    return template.replace("{name}", name)


def _distribute_reward(
    db: Session, campaign: Campaign, customer: Customer
) -> str | None:
    """派發活動獎勵（同一交易內，不 commit）；回傳 reward_ref 供記錄。

    - coupon：reward_value 為 coupon_id，對該顧客核銷一張（redeem_coupon）。
    - points：reward_value 為點數，earn_points 加點。
    無 reward_type 則略過回 None。核銷例外（已領/額滿等）視為非致命，回 None。
    """
    if not campaign.reward_type or campaign.reward_value is None:
        return None

    if campaign.reward_type == REWARD_POINTS:
        membership_svc.earn_points(
            db,
            tenant_id=campaign.tenant_id,
            customer=customer,
            delta=int(campaign.reward_value),
            reason=f"campaign:{campaign.id}",
        )
        return f"points:{campaign.reward_value}"

    if campaign.reward_type == REWARD_COUPON:
        coupon = db.execute(
            select(Coupon).where(
                Coupon.id == int(campaign.reward_value),
                Coupon.tenant_id == campaign.tenant_id,
            )
        ).scalar_one_or_none()
        if coupon is None or not customer.line_user_id:
            return None
        try:
            redemption = coupons_svc.redeem_coupon(
                db,
                tenant_id=campaign.tenant_id,
                code=coupon.code,
                line_user_id=customer.line_user_id,
                customer_id=customer.id,
            )
        except coupons_svc.CouponError:
            return None
        return f"coupon:{redemption.id}"

    return None


def run_campaign(
    db: Session,
    *,
    campaign: Campaign,
    now: datetime.datetime,
    cap: int,
    push_client: LinePushClient,
) -> dict:
    """執行一個活動：逐顧客 claim → 獎勵 → 推播 → 標 sent/failed。

    回傳 dict(sent, skipped)。每位顧客各自 try/except（per-customer isolation）。
    """
    sent = 0
    skipped = 0
    period_key = period_key_for(campaign, now)
    customers = eligible_customers(db, campaign, now)

    for customer in customers:
        if sent >= cap:
            break
        try:
            # 1. claim 一筆 CampaignSend（重複/上限由 unique 約束擋）。
            row = CampaignSend(
                tenant_id=campaign.tenant_id,
                campaign_id=campaign.id,
                customer_id=customer.id,
                line_user_id=customer.line_user_id,
                period_key=period_key,
                status=CAMPAIGN_SEND_PENDING,
            )
            db.add(row)
            try:
                db.flush()  # 觸發 unique；重複則 IntegrityError
            except IntegrityError:
                db.rollback()
                # 已存在同 period_key 的 send：若仍 pending（例如 welcome 觸發
                # 時預先入列的 claim），改採該既有列繼續發送；否則（已 sent/failed）
                # 視為冪等跳過。
                existing = db.execute(
                    select(CampaignSend).where(
                        CampaignSend.campaign_id == campaign.id,
                        CampaignSend.customer_id == customer.id,
                        CampaignSend.period_key == period_key,
                    )
                ).scalar_one_or_none()
                if existing is None or existing.status != CAMPAIGN_SEND_PENDING:
                    skipped += 1
                    continue
                row = existing

            # 2. 月度推播額度閘門：超出本月額度則跳過（不派獎勵、不推播、標
            #    skipped），並中止本活動其餘顧客（額度已罄，後續必同樣超額）。
            #    在派發獎勵前檢查，避免「發了券卻沒送出推播」的白扣。
            if not push_quota_svc.has_push_quota(db, campaign.tenant_id, now=now):
                row.status = CAMPAIGN_SEND_SKIPPED
                row.attempt_count = (row.attempt_count or 0) + 1
                row.last_error = "push allowance exceeded"
                db.commit()
                skipped += 1
                break

            # 3. 派發獎勵（同一交易內）。
            reward_ref = _distribute_reward(db, campaign, customer)
            row.reward_ref = reward_ref

            # 4. 推播訊息（無 line_user_id 不可推，標 failed）。
            text = _render(campaign.message_template, customer)
            if not customer.line_user_id:
                row.status = CAMPAIGN_SEND_FAILED
                row.attempt_count = (row.attempt_count or 0) + 1
                row.last_error = "no_line_user_id"
                db.commit()
                skipped += 1
                continue

            push_client.push(customer.line_user_id, text, access_token="")
            row.status = CAMPAIGN_SEND_SENT
            row.sent_at = now
            row.attempt_count = (row.attempt_count or 0) + 1
            db.commit()
            # 後扣：推播成功後才計量本月推播額度（只計實際送出者）。
            push_quota_svc.consume_push(db, campaign.tenant_id, now=now)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - per-customer failure must not stop batch
            db.rollback()
            # 在新交易把該 claim 標 failed（若已存在）。
            existing = db.execute(
                select(CampaignSend).where(
                    CampaignSend.campaign_id == campaign.id,
                    CampaignSend.customer_id == customer.id,
                    CampaignSend.period_key == period_key,
                )
            ).scalar_one_or_none()
            if existing is not None and existing.status == CAMPAIGN_SEND_PENDING:
                existing.status = CAMPAIGN_SEND_FAILED
                existing.attempt_count = (existing.attempt_count or 0) + 1
                existing.last_error = type(exc).__name__[:255]
                db.commit()
            skipped += 1

    return {"sent": sent, "skipped": skipped}


def maybe_enqueue_spend_for_customer(
    db: Session, tenant_id: int, customer: Customer
) -> int:
    """集點達門檻時入列 spend CampaignSend（內嵌觸發；不同步發送、不 commit）。

    對該租戶所有 active 的 spend 活動：若 customer.points_balance 達該活動的
    reward_value 門檻（reward_value 為 None 視為門檻 0），入列一筆 pending
    CampaignSend（period_key='YYYY'，冪等）。回傳實際入列筆數。
    """
    now = _utcnow()
    spends = list(
        db.execute(
            select(Campaign).where(
                Campaign.tenant_id == tenant_id,
                Campaign.type == "spend",
                Campaign.is_active.is_(True),
            )
        ).scalars()
    )
    balance = customer.points_balance or 0
    period_key = now.strftime("%Y")
    added = 0
    for campaign in spends:
        threshold = campaign.reward_value if campaign.reward_value is not None else 0
        if balance < threshold:
            continue
        row = CampaignSend(
            tenant_id=campaign.tenant_id,
            campaign_id=campaign.id,
            customer_id=customer.id,
            line_user_id=customer.line_user_id,
            period_key=period_key,
            status=CAMPAIGN_SEND_PENDING,
        )
        db.add(row)
        try:
            db.flush()
            added += 1
        except IntegrityError:
            db.rollback()
    return added


def create_welcome_send(db: Session, customer: Customer) -> int:
    """新顧客建檔時的 welcome 觸發（內嵌；不同步發送）。

    對該租戶所有 active 的 welcome 活動，各入列一筆 pending CampaignSend
    （period_key='once'，冪等），交由 cron/手動 run_campaign 派送。
    **不 commit**（與 customer 建檔同交易），回傳實際入列筆數。
    """
    welcomes = list(
        db.execute(
            select(Campaign).where(
                Campaign.tenant_id == customer.tenant_id,
                Campaign.type == CAMPAIGN_WELCOME,
                Campaign.is_active.is_(True),
            )
        ).scalars()
    )
    added = 0
    for campaign in welcomes:
        row = CampaignSend(
            tenant_id=campaign.tenant_id,
            campaign_id=campaign.id,
            customer_id=customer.id,
            line_user_id=customer.line_user_id,
            period_key="once",
            status=CAMPAIGN_SEND_PENDING,
        )
        db.add(row)
        try:
            db.flush()
            added += 1
        except IntegrityError:
            db.rollback()
    return added

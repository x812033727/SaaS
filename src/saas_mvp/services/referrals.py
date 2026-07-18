"""顧客推薦迴路(R11-B)。

* 每客一組 tenant 內唯一推薦碼(6 字,無易混淆字元)。
* 綁定:新客(尚無 referred_by)輸入他人碼 → referred_by_customer_id。
  不可自薦、不可換綁、碼須同租戶。
* 獎勵:被推薦客**首次標記到場**時,推薦人一次性獲得
  tenant loyalty 設定的 referral_points(referral_rewarded_at 冪等鎖)。
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 無 I/O/0/1
_CODE_LEN = 6


class ReferralError(ValueError):
    pass


def _new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))


def get_or_create_code(db: Session, customer: Customer) -> str:
    """取得(或產生)顧客推薦碼;產生即 flush(caller commit)。"""
    if customer.referral_code:
        return customer.referral_code
    for _ in range(5):
        code = _new_code()
        customer.referral_code = code
        try:
            db.flush()
            return code
        except IntegrityError:
            db.rollback()
            customer = db.get(Customer, customer.id)
            if customer.referral_code:
                return customer.referral_code
    raise ReferralError("推薦碼產生失敗,請稍後再試。")


def find_by_code(db: Session, *, tenant_id: int, code: str) -> Customer | None:
    normalized = (code or "").strip().upper()
    if len(normalized) != _CODE_LEN:
        return None
    return db.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.referral_code == normalized,
        )
    ).scalar_one_or_none()


def bind_by_code(db: Session, *, customer: Customer, code: str) -> Customer:
    """綁定推薦人(flush,caller commit)。回傳推薦人。"""
    if customer.referred_by_customer_id is not None:
        raise ReferralError("您已綁定過推薦人,無法更改。")
    referrer = find_by_code(db, tenant_id=customer.tenant_id, code=code)
    if referrer is None:
        raise ReferralError("推薦碼不存在,請確認後再試。")
    if referrer.id == customer.id:
        raise ReferralError("不能使用自己的推薦碼。")
    customer.referred_by_customer_id = referrer.id
    db.flush()
    return referrer


def reward_if_due(db: Session, reservation) -> None:
    """被推薦客到場 → 推薦人一次性得點(冪等;caller 負責 commit)。

    掛在「標記到場」的寫入點;任何前置條件不符皆靜默 no-op,
    不得影響到場標記本身。
    """
    if not reservation.attended or reservation.customer_id is None:
        return
    customer = db.get(Customer, reservation.customer_id)
    if (
        customer is None
        or customer.referred_by_customer_id is None
        or customer.referral_rewarded_at is not None
    ):
        return
    referrer = db.get(Customer, customer.referred_by_customer_id)
    if referrer is None or referrer.tenant_id != customer.tenant_id:
        return
    from saas_mvp.services import loyalty_config as loyalty_config_svc

    config = loyalty_config_svc.get_config(db, customer.tenant_id)
    points = getattr(config, "referral_points", None)
    if points is None:
        points = 50
    if points <= 0:
        return
    referrer.points_balance = (referrer.points_balance or 0) + points
    customer.referral_rewarded_at = datetime.datetime.now(datetime.timezone.utc)
    db.flush()

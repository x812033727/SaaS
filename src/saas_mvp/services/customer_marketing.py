"""顧客行銷退訂/同意(R6-B1,PDPA)。

opt-out 模型:`marketing_opt_out_at` NULL = 訂閱中;非 NULL = 已退訂。
**只影響行銷 broadcast/campaign 派送**;交易性通知(建單/提醒/取消/退款)
= 服務必要,恆送不可退。

退訂連結能力憑證 `unsubscribe_token`(不可猜 token_urlsafe(32),解析失敗一律
404 不洩漏存在性),惰性簽發比照 `portal_token`/`ics_token`。
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.customer import Customer


class UnsubscribeTokenNotFound(Exception):
    """退訂 token 解析失敗(不存在/長度異常);呼叫端一律 404 不洩漏存在性。"""


def is_opted_out(customer: Customer) -> bool:
    return customer.marketing_opt_out_at is not None


def assign_unsubscribe_token_if_missing(customer: Customer) -> None:
    """惰性配置退訂 token(不 commit;隨呼叫端交易提交,比照 portal 簽發-附掛分離)。"""
    if not customer.unsubscribe_token:
        customer.unsubscribe_token = secrets.token_urlsafe(32)


def unsubscribe_url(customer: Customer) -> str | None:
    """退訂連結;無 public_base_url 或 token 未簽發回 None(唯讀不簽發)。"""
    base = (settings.public_base_url or "").rstrip("/")
    if not base or not customer.unsubscribe_token:
        return None
    return f"{base}/unsubscribe/{customer.unsubscribe_token}"


def resolve_unsubscribe_token(db: Session, token: str) -> Customer:
    """token → Customer;不可猜、長度上限防掃描。找不到拋 UnsubscribeTokenNotFound。"""
    if not token or len(token) > 64:
        raise UnsubscribeTokenNotFound()
    customer = db.execute(
        select(Customer).where(Customer.unsubscribe_token == token)
    ).scalar_one_or_none()
    if customer is None:
        raise UnsubscribeTokenNotFound()
    return customer


def opt_out(db: Session, customer: Customer) -> Customer:
    """退訂行銷推播(冪等)。commit。"""
    if customer.marketing_opt_out_at is None:
        customer.marketing_opt_out_at = datetime.datetime.now(datetime.timezone.utc)
        db.commit()
    return customer


def opt_in(db: Session, customer: Customer) -> Customer:
    """重新訂閱(復訂,冪等)。commit。"""
    if customer.marketing_opt_out_at is not None:
        customer.marketing_opt_out_at = None
        db.commit()
    return customer


def unsubscribe_suffix(customer: Customer) -> str:
    """行銷訊息末尾的退訂提示行;無連結時回空字串(不附)。"""
    url = unsubscribe_url(customer)
    return f"\n\n若不想再收到行銷訊息,可點此退訂:{url}" if url else ""

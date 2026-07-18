"""E2E(R11-C):線上購卡整鏈 — 公開頁表單 → 綠界導頁 → 付款(模擬
callback)→ 狀態頁顯示一次性卡號。瀏覽器級驗證 R11-A 金流面。"""

from __future__ import annotations

import json
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.e2e

_PASSWORD = "e2e-gc-123"


def _register(api_base: str) -> tuple[str, str]:
    email = f"gc-{uuid.uuid4().hex[:8]}@example.com"
    store = f"GC {uuid.uuid4().hex[:6]}"
    req = urllib.request.Request(
        f"{api_base}/auth/register",
        data=json.dumps({
            "email": email, "password": _PASSWORD, "tenant_name": store,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 201
    return email, store


def _setup_sale(db_path: str, store: str) -> str:
    """直連 sqlite:發佈公開頁+開販售;回 slug。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from saas_mvp.models.business_profile import BusinessProfile
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import gift_card_sales as sales_svc

    db = sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))()
    try:
        tenant = db.execute(select(Tenant).where(Tenant.name == store)).scalar_one()
        slug = f"gc-{uuid.uuid4().hex[:8]}"
        db.add(BusinessProfile(tenant_id=tenant.id, slug=slug, is_published=True))
        sales_svc.save_config(
            db, tenant_id=tenant.id, online_sale_enabled=True,
            denominations=[500, 1000],
            fulfillment_guarantee="本店禮物卡依禮券定型化契約辦理履約保障。",
            updated_by_user_id=None,
        )
        db.commit()
        return slug
    finally:
        db.close()


def _pay_latest_order(db_path: str, store: str) -> str:
    """模擬 gateway callback:mark_order_paid;回 trade_no。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from saas_mvp.models.order import Order
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import shop as shop_svc

    db = sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))()
    try:
        tenant = db.execute(select(Tenant).where(Tenant.name == store)).scalar_one()
        order = (
            db.query(Order)
            .filter(Order.tenant_id == tenant.id)
            .order_by(Order.id.desc())
            .first()
        )
        shop_svc.mark_order_paid(
            db, tenant_id=tenant.id, order_id=order.id,
            provider="ecpay", provider_trade_no="E2E1",
        )
        return order.merchant_trade_no
    finally:
        db.close()


def test_public_purchase_to_code_display(server, page):
    _, store = _register(server["base"])
    slug = _setup_sale(server["db_path"], store)
    base = server["base"]

    # 1) 公開購卡頁
    page.goto(f"{base}/p/{slug}/gift-cards")
    assert "履約保障" in page.inner_text("body")
    # 2) 填表送出 → 綠界導頁(自動 POST 表單;不真送出外部)
    page.click('label:has-text("NT$1,000")')  # radio 本體 display:none,點卡片 label
    page.fill('input[name="purchaser_email"]', "buyer@example.com")
    page.fill('input[name="purchaser_name"]', "E2E 買家")
    page.check('input[name="agree"]')
    with page.expect_navigation():
        page.click('button[type="submit"]')
    assert "/payments/ecpay/checkout/" in page.url
    # 3) 模擬 gateway callback 付款成功
    trade_no = _pay_latest_order(server["db_path"], store)
    # 4) 狀態頁顯示一次性卡號
    page.goto(f"{base}/p/{slug}/gift-cards/{trade_no}")
    body = page.inner_text("body")
    assert "購買完成" in body
    assert "buyer@example.com" in body
    # 卡號格式 XXXX-XXXX-XXXX-XXXX
    import re

    assert re.search(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", body)

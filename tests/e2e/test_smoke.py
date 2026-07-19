"""E2E 冒煙:註冊 → 建時段 → 顧客表單無登入建單 → 後台看到預約。

種子(時段/表單 token)直接寫同一顆 sqlite — 冒煙標的是「瀏覽器端整條
預約路徑」,不是後台建時段 UI(有單元測試覆蓋)。
"""

from __future__ import annotations

import datetime

import pytest

pytestmark = pytest.mark.e2e

_EMAIL = "e2e-owner@example.com"
_PASSWORD = "e2e-password-123"
_STORE = "E2E 冒煙店"


def _seed(db_path: str) -> str:
    """建明日時段 + 發表單 token;回傳 token。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import booking_form as booking_form_svc

    engine = create_engine(f"sqlite:///{db_path}")
    db = sessionmaker(bind=engine)()
    try:
        tenant = db.execute(
            select(Tenant).where(Tenant.name == _STORE)
        ).scalar_one()
        tomorrow = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        ).replace(hour=10, minute=0, second=0, microsecond=0)
        db.add(BookingSlot(tenant_id=tenant.id, slot_start=tomorrow, max_capacity=4))
        db.commit()
        row = booking_form_svc.issue_token(
            db, tenant_id=tenant.id, line_user_id="Ue2e", display_name="冒煙顧客"
        )
        return row.token
    finally:
        db.close()


def test_booking_smoke_path(server, console_server, page):
    base = server["base"]

    # 1) 店家註冊(自動登入導向 dashboard)
    page.goto(f"{base}/ui/register")
    page.fill('input[name="email"]', _EMAIL)
    page.fill('input[name="password"]', _PASSWORD)
    page.fill('input[name="tenant_name"]', _STORE)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base}/ui/**")

    # 2) 種時段 + 表單 token(直接寫同顆 DB)
    token = _seed(server["db_path"])

    # 3) 顧客分頁:無登入走完表單(無服務 → 直接選日期)
    ctx2 = page.context.browser.new_context()
    guest = ctx2.new_page()
    try:
        guest.goto(f"{base}/booking/f/{token}")
        guest.click("a.choice")           # 選唯一日期
        guest.check('input[name="slot_id"]')
        guest.click('button[type="submit"]')
        assert "預約" in guest.content()
        assert guest.locator("text=完成").count() or "成功" in guest.content()
    finally:
        ctx2.close()

    # 4) 店家後台看到這筆預約(R12-C3a:/ui/booking 已刪,驗 console;
    #    console 是獨立 next server port,cookie 同 host 跨 port 共享)
    page.goto(f"{console_server['base']}/console/reservations")
    page.wait_for_selector("text=冒煙顧客", timeout=15000)

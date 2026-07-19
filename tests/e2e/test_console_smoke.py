"""E2E 冒煙(R4-C4):console 登入 → 儀表板 → 建預約 → /ui 同 session 可見。

驗證 console 整條路徑 + SSO 橋:console 登入種下的 cookie(同 127.0.0.1
不分 port 共享)讓 /ui(另一 port 的 uvicorn)免二次登入。

nightly only(pytestmark=e2e);需 console 已 build(cd frontend && npm run build)。
"""

from __future__ import annotations

import datetime
import json
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.e2e

_PASSWORD = "e2e-console-123"


def _register(api_base: str) -> str:
    """經 API 註冊一個 owner,回 tenant_name(store)。"""
    email = f"console-{uuid.uuid4().hex[:8]}@example.com"
    store = f"Console 冒煙 {uuid.uuid4().hex[:6]}"
    req = urllib.request.Request(
        f"{api_base}/auth/register",
        data=json.dumps(
            {"email": email, "password": _PASSWORD, "tenant_name": store}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 201
    return email, store


def _seed_slot(db_path: str, store: str) -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.tenant import Tenant

    db = sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))()
    try:
        tenant = db.execute(select(Tenant).where(Tenant.name == store)).scalar_one()
        tomorrow = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        ).replace(hour=11, minute=0, second=0, microsecond=0)
        db.add(BookingSlot(tenant_id=tenant.id, slot_start=tomorrow, max_capacity=4))
        db.commit()
    finally:
        db.close()


def test_console_login_create_reservation_visible_in_ui(server, console_server, page):
    email, store = _register(server["base"])
    _seed_slot(server["db_path"], store)
    console = console_server["base"]

    # 1) console 登入 → 儀表板
    page.goto(f"{console}/console/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', _PASSWORD)
    page.click('button:has-text("登入")')
    page.wait_for_url("**/console/dashboard", timeout=15000)
    assert "今日營運" in page.content()

    # 2) 建預約(店家代訂 dialog)
    page.goto(f"{console}/console/reservations")
    page.click("text=建立預約")
    page.wait_for_selector('select[name="slot_id"]', timeout=10000)
    page.select_option('select[name="slot_id"]', index=1)  # index 0 是「選擇時段」
    page.fill('input[name="display_name"]', "E2E 顧客")
    page.locator('button:has-text("建立預約")').last.click()
    page.wait_for_selector("text=E2E 顧客", timeout=10000)

    # 3) /ui 同 session 可用(SSO 橋 cookie 已於 console 登入種下,跨 port 共享)
    #    R12-C3a:/ui/booking 已刪 → 改以保留的 loyalty 設定頁驗免二次登入
    #    (預約可見性已在步驟 2 的 console 驗過)。
    page.goto(f"{server['base']}/ui/loyalty")
    assert "/ui/login" not in page.url  # 免二次登入
    assert "分級" in page.content() or "loyalty" in page.url

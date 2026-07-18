"""E2E(R11-C):console 全頁走查 — 瀏覽器級驗證 R7/R8 遷移頁真的活著。

抓的正是 CI 單元測試抓不到的整鏈缺陷類(proxy 路徑、basePath、
session/cookie);每頁斷言渲染出頁面專屬內容而非錯誤畫面。
"""

from __future__ import annotations

import json
import urllib.request
import uuid

import pytest

pytestmark = pytest.mark.e2e

_PASSWORD = "e2e-walk-123"


def _register(api_base: str) -> str:
    email = f"walk-{uuid.uuid4().hex[:8]}@example.com"
    req = urllib.request.Request(
        f"{api_base}/auth/register",
        data=json.dumps({
            "email": email, "password": _PASSWORD,
            "tenant_name": f"Walk {uuid.uuid4().hex[:6]}",
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 201
    return email

# (path, 頁面專屬字樣) — 出現即證明該頁資料鏈(proxy→API→render)通了
_PAGES = [
    ("/console/auto-reply", "新增規則"),
    ("/console/flex-menu", "卡片("),
    ("/console/rich-menu", "顧客對話視窗底部的固定選單"),
    ("/console/reports", "預約取消率"),
    ("/console/members", "邀請成員"),
    ("/console/account", "兩步驟驗證"),
    ("/console/plan", "目前方案"),
    ("/console/billing", "扣款明細"),
    ("/console/features", "扣款紀錄"),
    ("/console/gift-cards", "線上販售"),
    ("/console/commissions", "薪資結算單"),
    ("/console/resources", "資源類型"),
    ("/console/packages", "新增套票"),
    ("/console/client-forms", "新增表單範本"),
]


def test_console_full_page_walk(server, console_server, page):
    email = _register(server["base"])
    base = console_server["base"]

    page.goto(f"{base}/console/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', _PASSWORD)
    page.click('button:has-text("登入")')
    page.wait_for_url("**/console/dashboard**", timeout=15000)

    import time

    failures = []
    for path, marker in _PAGES:
        page.goto(f"{base}{path}")
        body = ""
        for _ in range(20):  # 最長 10s:輪詢 body 全文,避開 selector 可見性怪癖
            body = page.inner_text("body")
            if marker in body:
                break
            time.sleep(0.5)
        else:
            failures.append(
                f"{path}: 未見「{marker}」;body={body[:200].replace(chr(10), ' ')}"
            )
    assert not failures, "\n".join(failures)


def test_console_auto_reply_crud_via_browser(server, console_server, page):
    email = _register(server["base"])
    base = console_server["base"]
    page.goto(f"{base}/console/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', _PASSWORD)
    page.click('button:has-text("登入")')
    page.wait_for_url("**/console/dashboard**", timeout=15000)

    page.goto(f"{base}/console/auto-reply")
    page.fill('input[name="keyword"]', "營業時間")
    page.fill('textarea[name="reply_text"]', "每日 10:00-20:00")
    page.click("text=新增規則", strict=False)
    # 提交按鈕與標題同字 → 用 button 精確點
    page.click('button:has-text("新增規則")')
    page.wait_for_selector("text=每日 10:00-20:00", timeout=10000)

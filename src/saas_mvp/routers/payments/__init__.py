"""金流端點（綠界 ECPay）— 公開，無我方 JWT/rate-limit。

* GET  /payments/ecpay/checkout/{order_id} — 渲染自動 submit 的綠界付款表單。
* POST /payments/ecpay/callback            — 綠界 server 回調：先驗 CheckMacValue
  再標記訂單已付，回純文字 "1|OK"。

安全完全靠 CheckMacValue：回調只看 RtnCode 不驗簽會被偽造，故務必先驗簽 + 交叉驗金額。
冪等：綠界會重送直到收到 "1|OK"；mark_order_paid 已付為 no-op，仍回 "1|OK"。
"""

from __future__ import annotations

from saas_mvp.routers.payments._shared import router  # noqa: F401 — app.py 引用面

# ⚠️ 按原檔區段順序 import — 路由註冊順序不可變(P5 純搬移)。
from saas_mvp.routers.payments import orders_ecpay, subscriptions, deposits, orders_gateways  # noqa: E402, F401

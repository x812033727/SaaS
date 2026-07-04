"""LINE 預約對話指令解析（純函式、離線可測，仿 translation/commands.py）。

支援兩種輸入：
  * 文字訊息：英文 slash 指令 + 中文關鍵字。
  * postback：querystring（Rich Menu / quick-reply 按鈕回傳 event.postback.data）。

統一輸出 ``(action, params)``：
  action ∈ {"book", "slots", "my", "cancel", "help", None}
  params 為已解析的 dict（slot_id / party_size / reservation_id 為 int）。
  action=None 代表非預約指令（handler 回說明）。
"""

from __future__ import annotations

import re
import urllib.parse

# 文字指令 → action 對照（英文 slash + 中文關鍵字）。
# 以「開頭比對」處理帶參數指令（如 "/book 12 4"、"預約 12 4"）。
_TEXT_ALIASES: dict[str, str] = {
    "/book": "book",
    "預約": "book",
    "/slots": "slots",
    "時段": "slots",
    "查詢時段": "slots",
    "/my": "my",
    "我的預約": "my",
    "/cancel": "cancel",
    "取消": "cancel",
    "/help": "help",
    "說明": "help",
    # 圖文選單卡片（Flex carousel）
    "/menu": "menu",
    "選單": "menu",
    # P3 優惠券 / 會員
    "/coupons": "coupons",
    "優惠券": "coupons",
    "/redeem": "redeem",
    "兌換": "redeem",
    "/points": "points",
    "點數": "points",
    "我的點數": "points",
    # P4 商品銷售
    "/shop": "shop",
    "商品": "shop",
    "/buy": "buy",
    "購買": "buy",
    "/orders": "my_orders",
    "我的訂單": "my_orders",
}


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_party(value: int | None) -> int:
    """人數正規化：None/非正數一律夾為 1（防 party=-5 等惡意輸入打到 book_slot
    的 ValueError → 500）。"""
    if value is None or value < 1:
        return 1
    return value


def parse_booking_command(text: str) -> tuple[str | None, dict]:
    """解析文字訊息為 (action, params)。

    Examples::

        >>> parse_booking_command("/slots")
        ('slots', {})
        >>> parse_booking_command("/book 12 4")
        ('book', {'slot_id': 12, 'party_size': 4})
        >>> parse_booking_command("預約 12")
        ('book', {'slot_id': 12, 'party_size': 1})
        >>> parse_booking_command("/cancel 7")
        ('cancel', {'reservation_id': 7})
        >>> parse_booking_command("我的預約")
        ('my', {})
        >>> parse_booking_command("隨便打字")
        (None, {})
    """
    if not text:
        return None, {}
    stripped = text.strip()
    parts = stripped.split()
    if not parts:
        return None, {}

    head = parts[0]
    action = _TEXT_ALIASES.get(head)
    if action is None:
        return None, {}

    args = parts[1:]
    if action == "book":
        params: dict = {}
        if args:
            slot_id = _to_int(args[0])
            if slot_id is not None:
                params["slot_id"] = slot_id
        params["party_size"] = _clamp_party(_to_int(args[1]) if len(args) > 1 else None)
        return action, params
    if action == "cancel":
        params = {}
        if args:
            rid = _to_int(args[0])
            if rid is not None:
                params["reservation_id"] = rid
        return action, params
    if action == "redeem":
        params = {}
        if args:
            params["code"] = args[0]  # 券碼為字串
        return action, params
    if action == "buy":
        params = {}
        if args:
            pid = _to_int(args[0])
            if pid is not None:
                params["product_id"] = pid
        params["qty"] = (_to_int(args[1]) if len(args) > 1 else None) or 1
        # 第三個 token 為選用券碼：「購買 <商品> <數量> <券碼>」。
        if len(args) > 2 and args[2]:
            params["coupon"] = args[2]
        return action, params
    # slots / my / help / coupons / points / shop / my_orders 無參數
    return action, {}


def parse_postback_data(data: str) -> tuple[str | None, dict]:
    """解析 postback querystring 為 (action, params)。

    Examples::

        >>> parse_postback_data("action=book&slot_id=42&party=2")
        ('book', {'slot_id': 42, 'party_size': 2})
        >>> parse_postback_data("action=cancel&reservation_id=7")
        ('cancel', {'reservation_id': 7})
        >>> parse_postback_data("action=slots")
        ('slots', {})
        >>> parse_postback_data("garbage")
        (None, {})
    """
    if not data:
        return None, {}
    qs = urllib.parse.parse_qs(data, keep_blank_values=False)
    actions = qs.get("action")
    if not actions:
        return None, {}
    action = actions[0]
    if action not in {
        "book", "pick_service", "pick_date", "pick_staff", "pick_slot",
        "slots", "my", "cancel", "help", "menu",
        "coupons", "redeem", "points",
        "shop", "buy", "my_orders",
    }:
        return None, {}

    def _qint(key: str) -> int | None:
        return _to_int(qs[key][0]) if key in qs else None

    def _qdate(key: str) -> str | None:
        """取出 'YYYY-MM-DD' 字串；格式不符則丟棄（回 None）。"""
        if key not in qs:
            return None
        val = qs[key][0]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
            return val
        return None

    params: dict = {}
    if action == "book":
        if "slot_id" in qs:
            slot_id = _to_int(qs["slot_id"][0])
            if slot_id is not None:
                params["slot_id"] = slot_id
        party = _to_int(qs["party"][0]) if "party" in qs else None
        params["party_size"] = _clamp_party(party)
    elif action == "pick_service":
        # 引導式第一步結果：使用者選定服務項目。
        sid = _qint("service_id")
        if sid is not None:
            params["service_id"] = sid
    elif action == "pick_date":
        # 日期步驟結果：服務 + 日期（'YYYY-MM-DD'）。
        sid = _qint("service_id")
        if sid is not None:
            params["service_id"] = sid
        d = _qdate("date")
        if d is not None:
            params["date"] = d
    elif action == "pick_staff":
        # 員工步驟結果：服務 + 員工（staff_id 可缺，代表「不指定」）+ 日期前向狀態。
        sid = _qint("service_id")
        if sid is not None:
            params["service_id"] = sid
        stid = _qint("staff_id")
        if stid is not None:
            params["staff_id"] = stid
        d = _qdate("date")
        if d is not None:
            params["date"] = d
    elif action == "pick_slot":
        # 引導式：使用者已選時段（可帶 service_id / staff_id 前向狀態）。
        if "slot_id" in qs:
            slot_id = _to_int(qs["slot_id"][0])
            if slot_id is not None:
                params["slot_id"] = slot_id
        sid = _qint("service_id")
        if sid is not None:
            params["service_id"] = sid
        stid = _qint("staff_id")
        if stid is not None:
            params["staff_id"] = stid
        # party 僅在明確帶值時加入，維持既有 raw pick_slot 輸出形狀（{slot_id}）。
        if "party" in qs:
            party = _to_int(qs["party"][0])
            if party is not None:
                params["party_size"] = _clamp_party(party)
    elif action == "redeem":
        if "code" in qs:
            params["code"] = qs["code"][0]
    elif action == "buy":
        if "product_id" in qs:
            pid = _to_int(qs["product_id"][0])
            if pid is not None:
                params["product_id"] = pid
        qty = _to_int(qs["qty"][0]) if "qty" in qs else None
        params["qty"] = qty or 1
        if "coupon" in qs:
            params["coupon"] = qs["coupon"][0]
    elif action == "cancel":
        if "reservation_id" in qs:
            rid = _to_int(qs["reservation_id"][0])
            if rid is not None:
                params["reservation_id"] = rid
    return action, params

"""預約（booking）子套件 — LINE 對話指令解析。

對外 API::

    from saas_mvp.booking.commands import (
        parse_booking_command,   # 文字訊息 → (action, params)
        parse_postback_data,     # postback querystring → (action, params)
    )
"""

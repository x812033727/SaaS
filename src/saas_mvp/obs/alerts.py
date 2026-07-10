"""外部告警（B4）— Sentry 薄包裝，未設定/未安裝時靜默降級。

* ``init_sentry()``：app 啟動時呼叫一次；``SAAS_SENTRY_DSN`` 空或 sentry-sdk
  未安裝皆 no-op（後者記 warning）。sentry-sdk 為 prod extras 選用依賴。
* ``capture_alert(msg)``：關鍵營運事件（金流回調驗簽失敗、金額不符…）主動
  上報；Sentry 未啟用時退化為 error log，永不拋錯、永不阻擋主流程。
"""

from __future__ import annotations

import logging

from saas_mvp.config import settings

_log = logging.getLogger(__name__)
_enabled = False


def init_sentry() -> None:
    global _enabled
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            # 只收錯誤/訊息，不收 performance traces（省配額）。
            traces_sample_rate=0,
        )
        _enabled = True
        _log.info("sentry initialized (env=%s)", settings.env)
    except ImportError:
        _log.warning(
            "SAAS_SENTRY_DSN set but sentry-sdk not installed; "
            "pip install '.[prod]' 或 pip install sentry-sdk"
        )
    except Exception:  # noqa: BLE001 — 告警初始化失敗不得阻擋啟動
        _log.warning("sentry init failed", exc_info=True)


def capture_alert(message: str) -> None:
    """上報關鍵事件；Sentry 未啟用退化為 error log。永不拋錯。"""
    _log.error("ALERT: %s", message)
    if not _enabled:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level="error")
    except Exception:  # noqa: BLE001
        pass

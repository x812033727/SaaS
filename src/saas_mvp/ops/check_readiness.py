"""上線就緒檢查（C5）— 純唯讀,逐項 PASS/WARN/FAIL + 修復提示。

Usage:
    python -m saas_mvp.ops.check_readiness
    python -m saas_mvp.ops.check_readiness --json

檢查面向:核心安全、金流、發票、Email、Sentry、AI、DB migration、備份、
scheduler 心跳、LINE 設定。**預設不打任何網路**（純設定/檔案/DB 檢查）。

exit code:任何 FAIL → 1（WARN 不影響）。寫進 docs/GO_LIVE.md 的第一步。
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from typing import TextIO

from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models

_INSECURE_SECRET = "change-me-in-production-use-32-chars-min"
_DEV_LINE_KEY = "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc="
_ECPAY_TEST_MERCHANT = "2000132"


@dataclass(frozen=True)
class Check:
    name: str
    status: str   # PASS | WARN | FAIL
    detail: str

    def to_line(self) -> str:
        icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[self.status]
        return f"{icon} [{self.status}] {self.name} — {self.detail}"


def run_checks(
    *,
    session_factory=SessionLocal,
    db_engine=None,
    include_host_checks: bool = True,
) -> list[Check]:
    checks: list[Check] = []
    add = checks.append

    # ── 1. 核心安全 ─────────────────────────────────────────────
    if settings.secret_key == _INSECURE_SECRET:
        add(Check("secret_key", "FAIL", "仍是預設值;設 SAAS_SECRET_KEY(≥32 字)"))
    else:
        add(Check("secret_key", "PASS", "已自訂"))
    if settings.line_channel_encrypt_key == _DEV_LINE_KEY:
        add(Check("line_channel_encrypt_key", "FAIL",
                  "仍是 dev 預設;產生新 Fernet key 設 SAAS_LINE_CHANNEL_ENCRYPT_KEY"))
    else:
        add(Check("line_channel_encrypt_key", "PASS", "已自訂"))
    if settings.env in ("dev", "test"):
        add(Check("env", "WARN", f"env={settings.env}(正式環境應設 SAAS_ENV=prod)"))
    else:
        add(Check("env", "PASS", f"env={settings.env}"))
    if not settings.ui_csrf_enabled:
        add(Check("ui_csrf", "FAIL", "UI CSRF 已關閉;正式環境必須開啟"))
    else:
        add(Check("ui_csrf", "PASS", "開啟"))

    # ── 2. 金流 ────────────────────────────────────────────────
    try:
        from saas_mvp.services.platform_payment_config import effective_payment_config

        with session_factory() as db:
            payment_config = effective_payment_config(db, settings)
    except Exception as exc:  # noqa: BLE001
        payment_config = None
        add(Check("payment", "FAIL", f"無法讀取金流設定:{type(exc).__name__}"))

    if payment_config is None:
        pass
    elif payment_config.provider == "stub":
        add(Check(
            "payment",
            "WARN",
            f"provider=stub source={payment_config.source}(模擬付款,不會真收錢)",
        ))
    elif payment_config.provider == "ecpay":
        if (
            payment_config.environment == "prod"
            and payment_config.merchant_id == _ECPAY_TEST_MERCHANT
        ):
            add(Check("payment", "FAIL",
                      "ecpay_env=prod 但仍用綠界測試商店 2000132;填正式商店代號"))
        elif not (payment_config.hash_key and payment_config.hash_iv):
            add(Check("payment", "FAIL", "ecpay 缺 HashKey 或 HashIV"))
        elif payment_config.environment != "prod":
            add(Check(
                "payment",
                "WARN",
                f"ecpay stage 演練模式(merchant={payment_config.merchant_id} "
                f"source={payment_config.source})",
            ))
        else:
            add(Check(
                "payment",
                "PASS",
                f"ecpay prod(merchant={payment_config.merchant_id} "
                f"source={payment_config.source})",
            ))
        if not settings.public_base_url.startswith("https://"):
            add(Check("public_base_url", "FAIL",
                      "ecpay 模式需 https 的 SAAS_PUBLIC_BASE_URL(回調用)"))
        else:
            add(Check("public_base_url", "PASS", settings.public_base_url))
    elif payment_config.provider == "linepay":
        if not (settings.line_pay_channel_id and settings.line_pay_channel_secret):
            add(Check("payment", "FAIL",
                      "provider=linepay 但缺 SAAS_LINE_PAY_CHANNEL_ID/SECRET"
                      "(每筆結帳會 LinePayError)"))
        elif settings.line_pay_env != "prod":
            add(Check("payment", "WARN",
                      "linepay sandbox 演練模式(SAAS_LINE_PAY_ENV != prod)"))
        else:
            add(Check("payment", "PASS", f"linepay prod(channel={settings.line_pay_channel_id})"))
        # linepay confirm/cancel 回調 URL 同樣依賴 public_base_url。
        if not settings.public_base_url.startswith("https://"):
            add(Check("public_base_url", "FAIL",
                      "linepay 模式需 https 的 SAAS_PUBLIC_BASE_URL(回調用)"))
        else:
            add(Check("public_base_url", "PASS", settings.public_base_url))
    else:
        add(Check("payment", "WARN", f"provider={payment_config.provider}"))

    # ── 3. 發票 ────────────────────────────────────────────────
    try:
        from saas_mvp.services.platform_invoice_config import effective_invoice_config

        with session_factory() as db:
            invoice_config = effective_invoice_config(db, settings)
        if invoice_config.provider == "stub":
            add(Check("invoice", "WARN", "provider=stub(不會真開發票)"))
        elif not (
            invoice_config.merchant_id
            and invoice_config.hash_key
            and invoice_config.hash_iv
        ):
            add(Check("invoice", "FAIL", "ecpay 發票憑證不完整"))
        else:
            add(Check(
                "invoice",
                "PASS",
                f"ecpay {invoice_config.environment} source={invoice_config.source}",
            ))
    except Exception as exc:  # noqa: BLE001
        add(Check("invoice", "WARN", f"無法讀取發票設定:{type(exc).__name__}"))

    # ── 4. Email / Sentry / AI(空 → 退化行為 WARN)───────────────
    try:
        from saas_mvp.services.platform_email_config import effective_email_config

        with session_factory() as db:
            email_config = effective_email_config(db, settings)
        if email_config is None:
            add(Check("smtp", "WARN", "未設 SMTP(驗證信/重設密碼信不會真寄)"))
        else:
            add(Check(
                "smtp",
                "PASS",
                f"smtp={email_config.host}:{email_config.port} source={email_config.source}",
            ))
    except Exception as exc:  # noqa: BLE001
        add(Check("smtp", "WARN", f"無法讀取 SMTP 設定:{type(exc).__name__}"))
    try:
        from saas_mvp.services.platform_observability_config import (
            effective_observability_config,
        )

        with session_factory() as db:
            observability_config = effective_observability_config(db, settings)
        if observability_config is None:
            add(Check("sentry", "WARN", "Sentry 尚未設定(告警退化為 error log)"))
        else:
            add(Check("sentry", "PASS", f"DSN 已設 source={observability_config.source}"))
    except Exception as exc:  # noqa: BLE001
        add(Check("sentry", "WARN", f"無法讀取 Sentry 設定:{type(exc).__name__}"))
    try:
        from saas_mvp.services.platform_ai_config import effective_ai_config

        with session_factory() as db:
            ai_config = effective_ai_config(db, settings)
        if ai_config is None:
            add(Check("ai", "WARN", "AI 尚未設定；請到平台後台「AI 設定」完成"))
        else:
            add(Check(
                "ai",
                "PASS",
                f"provider={ai_config.provider} model={ai_config.model} "
                f"source={ai_config.source}",
            ))
    except Exception as exc:  # noqa: BLE001
        add(Check("ai", "WARN", f"無法讀取 AI 設定:{type(exc).__name__}"))

    # Google OAuth：後台加密設定優先，環境變數僅作備援。
    try:
        from saas_mvp.services.platform_oauth_config import (
            effective_google_credentials,
            google_status,
        )

        with session_factory() as db:
            credentials = effective_google_credentials(db, settings)
            source = google_status(db, settings)["source"]
        if credentials:
            add(Check("gcal_oauth", "PASS", f"Google OAuth 已設定 source={source}"))
        else:
            gcal_id = settings.google_oauth_client_id
            gcal_secret = settings.google_oauth_client_secret
            if gcal_id or gcal_secret:
                add(Check(
                    "gcal_oauth",
                    "FAIL",
                    "Google OAuth 環境備援只設定一半；請改由平台後台完整設定",
                ))
            else:
                add(Check(
                    "gcal_oauth",
                    "WARN",
                    "Google OAuth 尚未設定；請到平台後台「登入設定」完成",
                ))
    except Exception as exc:  # noqa: BLE001
        add(Check("gcal_oauth", "WARN", f"無法讀取 Google OAuth 設定:{type(exc).__name__}"))

    # 簡訊供應商三態:mitake 憑證齊=PASS;選了 mitake 但缺憑證=FAIL(設定矛盾);
    # stub=旗標開著才 WARN(避免營運者誤以為有真補送)。
    if settings.sms_provider == "mitake":
        if settings.mitake_username and settings.mitake_password:
            add(Check("sms", "PASS", "簡訊供應商:三竹 Mitake(憑證已設定)"))
        else:
            add(Check("sms", "FAIL",
                      "SAAS_SMS_PROVIDER=mitake 但 SAAS_MITAKE_USERNAME/PASSWORD 未填"
                      "(實際仍走 Stub,不真送簡訊)"))
    elif settings.sms_fallback_enabled:
        add(Check("sms", "WARN",
                  "SAAS_SMS_FALLBACK_ENABLED=true 但簡訊供應商僅 Stub(推播失敗只記 log,不真送簡訊)"))
    else:
        add(Check("sms", "PASS", "簡訊 fallback 關閉(預設)"))

    # ── 5. DB migration 到 head ─────────────────────────────────
    try:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        from saas_mvp.db import engine
        from saas_mvp.ops.migrate import _alembic_config

        script = ScriptDirectory.from_config(_alembic_config())
        head = script.get_current_head()
        effective_engine = db_engine if db_engine is not None else engine
        with effective_engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        if current == head:
            add(Check("db_migration", "PASS", f"alembic head={head}"))
        else:
            add(Check("db_migration", "FAIL", f"current={current} != head={head};跑 ops/migrate"))
    except Exception as exc:  # noqa: BLE001 — 檢查器本身不崩
        add(Check("db_migration", "WARN", f"無法確認 migration 狀態:{type(exc).__name__}"))

    if include_host_checks:
        # ── 6. 備份新鮮度 ────────────────────────────────────────
        backups = (
            sorted(pathlib.Path("backups").glob("*.dump"))
            if pathlib.Path("backups").is_dir()
            else []
        )
        if not backups:
            add(Check("backup", "WARN", "backups/ 無 dump(容器外執行時屬正常;請於主機驗證)"))
        else:
            age_h = (
                datetime.datetime.now().timestamp() - backups[-1].stat().st_mtime
            ) / 3600
            if age_h > 48:
                add(Check("backup", "FAIL", f"最新備份已 {age_h:.0f} 小時(>48h);檢查 db-backup 服務"))
            else:
                add(Check("backup", "PASS", f"最新備份 {age_h:.0f} 小時前({backups[-1].name})"))

        # ── 7. scheduler 心跳(容器內可見時)──────────────────────
        hb = pathlib.Path("/tmp/sched/heartbeat")
        if hb.exists():
            age_s = datetime.datetime.now().timestamp() - hb.stat().st_mtime
            if age_s > 180:
                add(Check("scheduler", "FAIL", f"心跳過舊({age_s:.0f}s);scheduler 可能卡死"))
            else:
                add(Check("scheduler", "PASS", f"心跳 {age_s:.0f}s 前"))
        else:
            add(Check("scheduler", "WARN", "看不到 /tmp/sched/heartbeat(非 scheduler 容器內屬正常)"))

    # ── 8. LINE 設定 ────────────────────────────────────────────
    try:
        from sqlalchemy import func, select

        from saas_mvp.models.line_channel_config import LineChannelConfig

        with session_factory() as db:
            cnt = db.execute(select(func.count(LineChannelConfig.id))).scalar_one()
        if cnt == 0:
            add(Check("line_config", "WARN", "尚無任何租戶綁定 LINE 官方帳號"))
        else:
            add(Check("line_config", "PASS", f"{cnt} 個租戶已綁定 LINE"))
    except Exception as exc:  # noqa: BLE001
        add(Check("line_config", "WARN", f"無法查詢:{type(exc).__name__}"))

    return checks


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    import_all_models()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    checks = run_checks()
    fails = sum(1 for c in checks if c.status == "FAIL")
    warns = sum(1 for c in checks if c.status == "WARN")

    if args.json:
        print(json.dumps(
            {"checks": [asdict(c) for c in checks], "fails": fails, "warns": warns},
            ensure_ascii=False, indent=2,
        ), file=out)
    else:
        for c in checks:
            print(c.to_line(), file=out)
        print(f"\n=== {len(checks)} 項:FAIL={fails} WARN={warns} "
              f"PASS={len(checks) - fails - warns} ===", file=out)
        if fails:
            print("有 FAIL 項目 — 修復後再上線。", file=out)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

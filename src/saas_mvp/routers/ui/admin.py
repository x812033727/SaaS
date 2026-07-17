"""UI 子模組(P4 純搬移自 routers/ui.py):平台管理。"""
from __future__ import annotations


from fastapi import Depends, Form, Query, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.deps import (
    Actor,
    get_db,
    require_ui_admin,
    require_ui_user,
)
from saas_mvp.auth.dependencies import _UI_COOKIE_NAME
from saas_mvp.auth.security import create_access_token
from saas_mvp.line_client import (
    LineBotInfoClient,
    get_bot_info_client,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.quota import get_quota_status
from saas_mvp.services import admin as admin_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import audit as audit_svc
from saas_mvp.services import line_config as line_config_svc
from saas_mvp.services import platform_oauth_config as platform_oauth_svc
from saas_mvp.services import platform_email_config as platform_email_svc
from saas_mvp.services import platform_ai_config as platform_ai_svc
from saas_mvp.services import (
    platform_observability_config as platform_observability_svc,
)
from saas_mvp.services import platform_payment_config as platform_payment_svc
from saas_mvp.services import platform_invoice_config as platform_invoice_svc
from saas_mvp.services import readiness_dashboard as readiness_dashboard_svc
from saas_mvp.services import invoice_profiles as invoice_profiles_svc
from saas_mvp.services.mailer import Mailer, MailerError, get_mailer
from fastapi import HTTPException

from saas_mvp.routers.ui._shared import (
    router, templates, _ctx, _is_htmx, _line_config_or_none, _line_webhook_url_for, _set_auth_cookie,
)
from saas_mvp.routers.ui.account import _oauth_callback_base

# ── 平台管理 ────────────────────────────────────────────────────────────────


@router.get("/admin", response_class=HTMLResponse)
def admin_overview(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """平台總覽（B4）：租戶/方案分佈/試用/MRR/本月扣款。"""
    readiness = readiness_dashboard_svc.build_dashboard(db)
    return templates.TemplateResponse(
        "admin/overview.html",
        _ctx(
            request,
            actor,
            overview=admin_svc.platform_overview(db),
            readiness=readiness,
        ),
    )


@router.get("/admin/ops", response_class=HTMLResponse)
def admin_ops(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """營運總覽(R4-P2):MRR/扣款成功率/即將續扣 + 租戶健康表。"""
    return templates.TemplateResponse(
        "admin/ops.html",
        _ctx(
            request,
            actor,
            revenue=admin_svc.revenue_overview(db),
            health_rows=admin_svc.tenant_health_rows(db),
        ),
    )


@router.get("/admin/readiness", response_class=HTMLResponse)
def admin_readiness(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """平台上線檢查中心：將技術檢查轉成可理解、可操作的後台頁面。"""
    return templates.TemplateResponse(
        "admin/readiness.html",
        _ctx(request, actor, readiness=readiness_dashboard_svc.build_dashboard(db)),
    )


def _platform_oauth_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    callback_base = _oauth_callback_base(request)
    return _ctx(
        request,
        actor,
        line_status=platform_oauth_svc.line_status(db, settings),
        google_status=platform_oauth_svc.google_status(db, settings),
        line_callback_url=f"{callback_base}/auth/oauth/line/callback",
        google_login_callback_url=f"{callback_base}/auth/oauth/google/callback",
        google_calendar_callback_url=f"{callback_base}/ui/gcal/callback",
        **extra,
    )


@router.get("/admin/oauth-settings", response_class=HTMLResponse)
def admin_oauth_settings(
    request: Request,
    saved: int = Query(0),
    google_saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """平台共用 LINE / Google OAuth；只有平台管理員可讀取或修改。"""
    return templates.TemplateResponse(
        "admin/oauth_settings.html",
        _platform_oauth_ctx(
            request,
            actor,
            db,
            saved=bool(saved),
            google_saved=bool(google_saved),
        ),
    )


@router.post("/admin/oauth-settings/line", response_class=HTMLResponse)
def admin_oauth_settings_save(
    request: Request,
    channel_id: str = Form(..., max_length=255),
    channel_secret: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_oauth_svc.save_line_credentials(
            db,
            channel_id=channel_id,
            channel_secret=channel_secret,
            actor_user_id=actor.user.id,
        )
    except platform_oauth_svc.PlatformOAuthConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/oauth_settings.html",
            _platform_oauth_ctx(request, actor, db, line_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.oauth.line.update",
        target="oauth:line",
        detail={
            "channel_id": channel_id.strip(),
            "secret_changed": bool(channel_secret),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/oauth-settings/line/reset", response_class=HTMLResponse)
def admin_oauth_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_oauth_svc.clear_line_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.oauth.line.reset",
            target="oauth:line",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/oauth-settings/google", response_class=HTMLResponse)
def admin_google_oauth_settings_save(
    request: Request,
    client_id: str = Form(..., max_length=255),
    client_secret: str = Form("", max_length=255),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_oauth_svc.save_google_credentials(
            db,
            client_id=client_id,
            client_secret=client_secret,
            actor_user_id=actor.user.id,
        )
    except platform_oauth_svc.PlatformOAuthConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/oauth_settings.html",
            _platform_oauth_ctx(request, actor, db, google_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.oauth.google.update",
        target="oauth:google",
        detail={"client_id": client_id.strip(), "secret_changed": bool(client_secret)},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings?google_saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/oauth-settings/google/reset", response_class=HTMLResponse)
def admin_google_oauth_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_oauth_svc.clear_google_override(db)
    if removed:
        if platform_oauth_svc.effective_google_credentials(db, settings) is None:
            from saas_mvp.models.tenant_gcal_credential import (
                GCAL_ERROR,
                TenantGcalCredential,
            )

            db.query(TenantGcalCredential).update(
                {
                    TenantGcalCredential.status: GCAL_ERROR,
                    TenantGcalCredential.last_error: (
                        "平台 Google OAuth 設定已移除，請聯絡平台管理員"
                    ),
                }
            )
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.oauth.google.reset",
            target="oauth:google",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/oauth-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_email_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    from saas_mvp.services import email_delivery as delivery_svc

    return _ctx(
        request,
        actor,
        email_status=platform_email_svc.email_status(db, settings),
        email_delivery_summary=delivery_svc.summary(db),
        email_deliveries=delivery_svc.recent(db),
        **extra,
    )


@router.get("/admin/email-settings", response_class=HTMLResponse)
def admin_email_settings(
    request: Request,
    saved: int = Query(0),
    retry_queued: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/email_settings.html",
        _platform_email_ctx(
            request, actor, db, saved=bool(saved), retry_queued=bool(retry_queued)
        ),
    )


@router.post("/admin/email-settings", response_class=HTMLResponse)
def admin_email_settings_save(
    request: Request,
    smtp_host: str = Form(..., max_length=255),
    smtp_port: int = Form(587),
    smtp_user: str = Form("", max_length=255),
    smtp_password: str = Form("", max_length=255),
    smtp_from: str = Form(..., max_length=255),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_email_svc.save_email_config(
            db,
            host=smtp_host,
            port=smtp_port,
            user=smtp_user,
            password=smtp_password,
            from_address=smtp_from,
            actor_user_id=actor.user.id,
        )
    except platform_email_svc.PlatformEmailConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/email_settings.html",
            _platform_email_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.email.update",
        target="email:smtp",
        detail={"host": smtp_host.strip(), "port": smtp_port},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/email-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/email-settings/test", response_class=HTMLResponse)
def admin_email_settings_test(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    mailer: Mailer = Depends(get_mailer),
):
    try:
        mailer.send(
            to=actor.user.email,
            subject="寄信設定測試 — LINE 預約平台",
            body="這是一封平台 SMTP 設定測試信。若你收到此信，代表寄信服務設定成功。",
        )
    except MailerError as exc:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.email.test",
            target="email:smtp",
            detail={"result": "failed", "reason": str(exc)},
            request=request,
        )
        db.commit()
        return templates.TemplateResponse(
            "admin/email_settings.html",
            _platform_email_ctx(
                request, actor, db, test_error=f"測試信寄送失敗：{exc}"
            ),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.email.test",
        target="email:smtp",
        detail={"result": "sent"},
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/email_settings.html",
        _platform_email_ctx(request, actor, db, test_sent=True),
    )


@router.post("/admin/email-settings/reset", response_class=HTMLResponse)
def admin_email_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_email_svc.clear_email_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.email.reset",
            target="email:smtp",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/email-settings", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/email-settings/retry", response_class=HTMLResponse)
def admin_email_settings_retry(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import email_delivery as delivery_svc

    count = delivery_svc.retry_unsent(db)
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.email.retry",
        target="email:outbox",
        detail={"count": count},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/email-settings?retry_queued=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_ai_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    return _ctx(
        request,
        actor,
        ai_status=platform_ai_svc.ai_status(db, settings),
        **extra,
    )


@router.get("/admin/ai-settings", response_class=HTMLResponse)
def admin_ai_settings(
    request: Request,
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/ai_settings.html",
        _platform_ai_ctx(request, actor, db, saved=bool(saved)),
    )


@router.post("/admin/ai-settings", response_class=HTMLResponse)
def admin_ai_settings_save(
    request: Request,
    api_key: str = Form("", max_length=255),
    model: str = Form(..., max_length=128),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_ai_svc.save_ai_config(
            db,
            api_key=api_key,
            model=model,
            actor_user_id=actor.user.id,
        )
    except platform_ai_svc.PlatformAIConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/ai_settings.html",
            _platform_ai_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.ai.update",
        target="ai:minimax",
        detail={"model": model.strip(), "key_changed": bool(api_key.strip())},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/ai-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/ai-settings/test", response_class=HTMLResponse)
def admin_ai_settings_test(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_ai_svc.test_ai_config(db, settings)
    except platform_ai_svc.PlatformAIConfigError as exc:
        return templates.TemplateResponse(
            "admin/ai_settings.html",
            _platform_ai_ctx(request, actor, db, test_error=str(exc)),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.ai.test",
        target="ai:minimax",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/ai_settings.html",
        _platform_ai_ctx(request, actor, db, test_ok=True),
    )


@router.post("/admin/ai-settings/reset", response_class=HTMLResponse)
def admin_ai_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_ai_svc.clear_ai_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.ai.reset",
            target="ai:minimax",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/ai-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_observability_ctx(
    request: Request, actor: Actor, db: Session, **extra
) -> dict:
    return _ctx(
        request,
        actor,
        observability_status=platform_observability_svc.observability_status(
            db, settings
        ),
        **extra,
    )


@router.get("/admin/observability-settings", response_class=HTMLResponse)
def admin_observability_settings(
    request: Request,
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/observability_settings.html",
        _platform_observability_ctx(request, actor, db, saved=bool(saved)),
    )


@router.post("/admin/observability-settings", response_class=HTMLResponse)
def admin_observability_settings_save(
    request: Request,
    sentry_dsn: str = Form(..., max_length=1024),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_observability_svc.save_observability_config(
            db, sentry_dsn=sentry_dsn, actor_user_id=actor.user.id
        )
    except platform_observability_svc.PlatformObservabilityConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/observability_settings.html",
            _platform_observability_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.observability.update",
        target="observability:sentry",
        request=request,
    )
    db.commit()
    platform_observability_svc.apply_effective_observability_config(db, settings)
    return RedirectResponse(
        "/ui/admin/observability-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/observability-settings/test", response_class=HTMLResponse)
def admin_observability_settings_test(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_observability_svc.send_test_event(db, settings)
    except platform_observability_svc.PlatformObservabilityConfigError as exc:
        return templates.TemplateResponse(
            "admin/observability_settings.html",
            _platform_observability_ctx(request, actor, db, test_error=str(exc)),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.observability.test",
        target="observability:sentry",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/observability_settings.html",
        _platform_observability_ctx(request, actor, db, test_ok=True),
    )


@router.post("/admin/observability-settings/reset", response_class=HTMLResponse)
def admin_observability_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    removed = platform_observability_svc.clear_observability_override(db)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.observability.reset",
            target="observability:sentry",
            request=request,
        )
    db.commit()
    platform_observability_svc.apply_effective_observability_config(db, settings)
    return RedirectResponse(
        "/ui/admin/observability-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_payment_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    from saas_mvp.models.feature_subscription import (
        SUB_ACTIVE,
        SUB_CANCEL_FAILED,
        SUB_PENDING,
        FeatureSubscription,
    )

    base = settings.public_base_url.rstrip("/")
    unsettled = (
        db.query(FeatureSubscription)
        .filter(
            FeatureSubscription.status.in_((SUB_PENDING, SUB_ACTIVE, SUB_CANCEL_FAILED))
        )
        .count()
    )
    return _ctx(
        request,
        actor,
        payment_status=platform_payment_svc.payment_status(db, settings),
        payment_public_base_url=base,
        payment_callbacks={
            "order": f"{base}/payments/ecpay/callback",
            "subscription": f"{base}/payments/ecpay/subscribe-callback",
            "period": f"{base}/payments/ecpay/period-callback",
            "deposit": f"{base}/payments/ecpay/deposit-callback",
        },
        unsettled_subscriptions=unsettled,
        refundable_deposits=platform_payment_svc.refundable_deposit_count(db),
        **extra,
    )


@router.get("/admin/payment-settings", response_class=HTMLResponse)
def admin_payment_settings(
    request: Request,
    saved: int = Query(0),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/payment_settings.html",
        _platform_payment_ctx(request, actor, db, saved=bool(saved)),
    )


@router.post("/admin/payment-settings/ecpay", response_class=HTMLResponse)
def admin_payment_settings_save(
    request: Request,
    merchant_id: str = Form(..., max_length=64),
    hash_key: str = Form("", max_length=128),
    hash_iv: str = Form("", max_length=128),
    environment: str = Form(..., max_length=16),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_payment_svc.save_ecpay_config(
            db,
            merchant_id=merchant_id,
            hash_key=hash_key,
            hash_iv=hash_iv,
            environment=environment,
            actor_user_id=actor.user.id,
            public_base_url=settings.public_base_url,
        )
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.payment.ecpay.update",
        target="payment:ecpay",
        detail={
            "merchant_id": merchant_id.strip(),
            "environment": environment.strip().lower(),
            "hash_key_changed": bool(hash_key.strip()),
            "hash_iv_changed": bool(hash_iv.strip()),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/payment-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/payment-settings/check", response_class=HTMLResponse)
def admin_payment_settings_check(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_payment_svc.self_check(db, settings)
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, check_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.payment.ecpay.check",
        target="payment:ecpay",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/payment_settings.html",
        _platform_payment_ctx(request, actor, db, check_ok=True),
    )


@router.post("/admin/payment-settings/disable", response_class=HTMLResponse)
def admin_payment_settings_disable(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_payment_svc.disable_payment(db, actor_user_id=actor.user.id)
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.payment.disable",
        target="payment:stub",
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/payment-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/payment-settings/reset", response_class=HTMLResponse)
def admin_payment_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        removed = platform_payment_svc.clear_payment_override(db)
    except platform_payment_svc.PlatformPaymentConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/payment_settings.html",
            _platform_payment_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_409_CONFLICT,
        )
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.payment.reset",
            target="payment:environment",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/payment-settings",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _platform_invoice_ctx(
    request: Request,
    actor: Actor,
    db: Session,
    **extra,
) -> dict:
    from sqlalchemy import func
    from saas_mvp.models.invoice import Invoice

    counts = dict(
        db.query(Invoice.status, func.count(Invoice.id)).group_by(Invoice.status).all()
    )
    invoices = db.query(Invoice).order_by(Invoice.id.desc()).limit(50).all()
    return _ctx(
        request,
        actor,
        invoice_status=platform_invoice_svc.invoice_status(db, settings),
        invoice_counts={
            "pending": counts.get("pending", 0),
            "issued": counts.get("issued", 0),
            "failed": counts.get("failed", 0),
            "voiding": counts.get("voiding", 0),
            "void": counts.get("void", 0),
        },
        invoices=invoices,
        invoice_buyer_summaries={
            row.id: invoice_profiles_svc.invoice_buyer_summary(row) for row in invoices
        },
        **extra,
    )


@router.get("/admin/invoice-settings", response_class=HTMLResponse)
def admin_invoice_settings(
    request: Request,
    saved: int = Query(0),
    retried: int = Query(-1),
    voided: str = Query("", max_length=16),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin/invoice_settings.html",
        _platform_invoice_ctx(
            request,
            actor,
            db,
            saved=bool(saved),
            retried=None if retried < 0 else retried,
            voided=voided,
        ),
    )


@router.post("/admin/invoice-settings/ecpay", response_class=HTMLResponse)
def admin_invoice_settings_save(
    request: Request,
    merchant_id: str = Form(..., max_length=64),
    hash_key: str = Form("", max_length=128),
    hash_iv: str = Form("", max_length=128),
    environment: str = Form(..., max_length=16),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_invoice_svc.save_ecpay_config(
            db,
            merchant_id=merchant_id,
            hash_key=hash_key,
            hash_iv=hash_iv,
            environment=environment,
            actor_user_id=actor.user.id,
        )
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.ecpay.update",
        target="invoice:ecpay",
        detail={
            "merchant_id": merchant_id.strip(),
            "environment": environment.strip().lower(),
            "hash_key_changed": bool(hash_key.strip()),
            "hash_iv_changed": bool(hash_iv.strip()),
        },
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/invoice-settings?saved=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/invoice-settings/check", response_class=HTMLResponse)
def admin_invoice_settings_check(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_invoice_svc.self_check(db, settings)
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, check_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.ecpay.check",
        target="invoice:ecpay",
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "admin/invoice_settings.html",
        _platform_invoice_ctx(request, actor, db, check_ok=True),
    )


@router.post("/admin/invoice-settings/retry", response_class=HTMLResponse)
def admin_invoice_settings_retry(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    from saas_mvp.models.invoice import INVOICE_FAILED, Invoice
    from saas_mvp.services.invoices import _attempt_issue

    rows = db.query(Invoice).filter(Invoice.status == INVOICE_FAILED).all()
    for row in rows:
        _attempt_issue(db, row)
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.retry",
        target="invoice:failed",
        detail={"count": len(rows)},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        f"/ui/admin/invoice-settings?retried={len(rows)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _invoice_config_error_response(request, actor, db, exc):
    db.rollback()
    return templates.TemplateResponse(
        "admin/invoice_settings.html",
        _platform_invoice_ctx(request, actor, db, error=str(exc)),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@router.post("/admin/invoice-settings/disable", response_class=HTMLResponse)
def admin_invoice_settings_disable(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        platform_invoice_svc.disable_invoice(db, actor_user_id=actor.user.id)
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        return _invoice_config_error_response(request, actor, db, exc)
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.disable",
        target="invoice:ecpay",
        request=request,
    )
    db.commit()
    return RedirectResponse(
        "/ui/admin/invoice-settings", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/invoice-settings/reset", response_class=HTMLResponse)
def admin_invoice_settings_reset(
    request: Request,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        removed = platform_invoice_svc.clear_invoice_override(db)
    except platform_invoice_svc.PlatformInvoiceConfigError as exc:
        return _invoice_config_error_response(request, actor, db, exc)
    if removed:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.invoice.reset",
            target="invoice:ecpay",
            request=request,
        )
    db.commit()
    return RedirectResponse(
        "/ui/admin/invoice-settings", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/admin/invoice-settings/{invoice_id}/void", response_class=HTMLResponse)
def admin_invoice_void(
    invoice_id: int,
    request: Request,
    reason: str = Form(..., min_length=1, max_length=20),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    from saas_mvp.services import invoices as invoices_svc

    try:
        row = invoices_svc.void_invoice(db, invoice_id, reason=reason)
    except invoices_svc.InvoiceProviderError as exc:
        audit_svc.record_from_actor(
            db,
            actor,
            action="platform.invoice.void_failed",
            target=f"invoice:{invoice_id}",
            detail={"reason": reason.strip(), "error": str(exc)[:255]},
            request=request,
        )
        db.commit()
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, operation_error=str(exc)),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    except invoices_svc.InvoiceOperationError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin/invoice_settings.html",
            _platform_invoice_ctx(request, actor, db, operation_error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="platform.invoice.void",
        target=f"invoice:{row.id}",
        detail={"invoice_no": row.invoice_no, "reason": row.void_reason},
        request=request,
    )
    db.commit()
    return RedirectResponse(
        f"/ui/admin/invoice-settings?voided={row.invoice_no}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    tenant_id: int | None = Query(None),
    action: str = Query(""),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """稽核日誌檢視（F1）：篩 tenant/action、分頁。"""
    from saas_mvp.models.audit_log import AuditLog

    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    if action.strip():
        stmt = stmt.where(AuditLog.action.like(f"{action.strip()}%"))
    rows = db.execute(stmt.offset(skip).limit(limit)).scalars().all()
    ctx = _ctx(
        request,
        actor,
        rows=rows,
        filters={
            "tenant_id": tenant_id,
            "action": action,
            "skip": skip,
            "limit": limit,
        },
    )
    template = "admin/_audit_table.html" if _is_htmx(request) else "admin/audit.html"
    return templates.TemplateResponse(template, ctx)


@router.post("/admin/tenants/{tenant_id}/impersonate", response_class=HTMLResponse)
def admin_impersonate(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    """代管（F2）:以該租戶 owner 身分開 30 分鐘短票 session。

    安全:拒絕代管 admin(禁權限橫向移動)、拒絕鏈式代管、audit start、
    代管票 actor=owner 天然進不了 /ui/admin。
    """
    if actor.impersonator_user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="已在代管中,不可鏈式代管"
        )
    target_owner = (
        db.execute(
            select(User)
            .where(
                User.tenant_id == tenant_id,
                User.role == "owner",
            )
            .order_by(User.id)
        )
        .scalars()
        .first()
    )
    if target_owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="該租戶沒有 owner 帳號"
        )
    if target_owner.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="不可代管平台管理員帳號"
        )

    audit_svc.record(
        db,
        action="impersonation.start",
        actor_user_id=target_owner.id,
        impersonator_user_id=actor.user.id,
        tenant_id=tenant_id,
        target=f"user:{target_owner.id}",
    )
    db.commit()
    token = create_access_token(
        user_id=target_owner.id,
        tenant_id=tenant_id,
        impersonator_id=actor.user.id,
    )
    resp = RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(resp, token)
    return resp


@router.post("/impersonation/stop", response_class=HTMLResponse)
def impersonation_stop(
    actor: Actor = Depends(require_ui_user),
    db: Session = Depends(get_db),
):
    """結束代管:以 imp 身分重簽正常 admin token(再驗仍是 admin)覆寫 cookie。"""
    if actor.impersonator_user_id is None:
        return RedirectResponse("/ui/", status_code=status.HTTP_303_SEE_OTHER)
    admin_user = db.get(User, actor.impersonator_user_id)
    if admin_user is None or not admin_user.is_admin:
        # fail-closed:admin 已失效 → 直接登出
        resp = RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(_UI_COOKIE_NAME, path="/")
        return resp
    audit_svc.record(
        db,
        action="impersonation.stop",
        actor_user_id=actor.user.id,
        impersonator_user_id=admin_user.id,
        tenant_id=actor.user.tenant_id,
    )
    db.commit()
    token = create_access_token(user_id=admin_user.id, tenant_id=admin_user.tenant_id)
    resp = RedirectResponse(
        f"/ui/admin/tenants/{actor.user.tenant_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_auth_cookie(resp, token)
    return resp


@router.get("/admin/bots", response_class=HTMLResponse)
def admin_bots(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    store_type: str | None = Query(None),
    is_active: bool | None = Query(None),
    uncategorized: bool = Query(False),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    # 空字串 store_type 視為「未指定」
    store_type = store_type or None
    rows = admin_svc.list_line_bots(
        db,
        skip=skip,
        limit=limit,
        store_type=store_type,
        is_active=is_active,
        uncategorized=uncategorized,
    )
    filters = {
        "store_type": store_type or "",
        "is_active": is_active,
        "uncategorized": uncategorized,
        "skip": skip,
        "limit": limit,
    }
    ctx = _ctx(request, actor, rows=rows, filters=filters)
    # HTMX 篩選請求只回表格 partial
    template = "admin/_bots_table.html" if _is_htmx(request) else "admin/bots.html"
    return templates.TemplateResponse(template, ctx)


@router.get("/admin/tenants/{tenant_id}", response_class=HTMLResponse)
def admin_tenant_detail(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        return templates.TemplateResponse(
            "admin/tenant_detail.html",
            _ctx(request, actor, tenant=None, not_found=True, tenant_id=tenant_id),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    usage = get_quota_status(db, tenant_id, tenant.plan)
    cfg = _line_config_or_none(db, tenant_id)
    return templates.TemplateResponse(
        "admin/tenant_detail.html",
        _ctx(
            request,
            actor,
            tenant=tenant,
            usage=usage,
            cfg=cfg,
            features=features_svc.list_for_tenant(db, tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
    )


@router.post(
    "/admin/tenants/{tenant_id}/features/{feature}", response_class=HTMLResponse
)
def admin_set_feature(
    request: Request,
    tenant_id: int,
    feature: str,
    enabled: str = Form(...),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        features_svc.validate_feature(feature)
        features_svc.set_enabled(
            db,
            tenant_id,
            feature,
            enabled == "true",
            actor_user_id=actor.user.id,
            source="admin",
        )
        audit_svc.record_from_actor(
            db,
            actor,
            action="admin.feature.set",
            target=f"tenant:{tenant_id}",
            detail={"feature": feature, "enabled": enabled == "true"},
            request=request,
        )
        db.commit()  # set_enabled 已自行 commit;這筆補稽核
    except features_svc.UnknownFeatureError:
        pass
    return templates.TemplateResponse(
        "admin/_tenant_features.html",
        _ctx(
            request,
            actor,
            tenant_id=tenant_id,
            features=features_svc.list_for_tenant(db, tenant_id),
        ),
    )


@router.post("/admin/tenants/{tenant_id}/patch", response_class=HTMLResponse)
def admin_tenant_patch(
    request: Request,
    tenant_id: int,
    plan: str = Form(...),
    is_active: str = Form(...),
    store_type: str = Form("", max_length=32),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        admin_svc.patch_tenant(
            db,
            tenant_id,
            is_active=(is_active == "true"),
            plan=plan,
            actor_user_id=actor.user.id,
            store_type=store_type,
            store_type_provided=True,  # 表單一律帶 store_type 欄位
        )
    except HTTPException as exc:
        tenant = db.get(Tenant, tenant_id)
        return templates.TemplateResponse(
            "admin/_tenant_summary.html",
            _ctx(request, actor, tenant=tenant, error=str(exc.detail)),
            status_code=exc.status_code,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="admin.tenant.patch",
        target=f"tenant:{tenant_id}",
        detail={
            "plan": plan,
            "is_active": is_active == "true",
            "store_type": store_type,
        },
        request=request,
    )
    db.commit()  # patch_tenant 已自行 commit;這筆補稽核
    tenant = db.get(Tenant, tenant_id)
    return templates.TemplateResponse(
        "admin/_tenant_summary.html",
        _ctx(request, actor, tenant=tenant, saved=True),
    )


@router.post("/admin/tenants/{tenant_id}/line-config", response_class=HTMLResponse)
def admin_line_config_save(
    request: Request,
    tenant_id: int,
    channel_secret: str = Form(..., max_length=64),
    access_token: str = Form(..., max_length=1024),
    default_target_lang: str = Form("zh-TW"),
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    try:
        cfg = line_config_svc.upsert_line_config(
            db,
            tenant_id,
            channel_secret=channel_secret,
            access_token=access_token,
            default_target_lang=default_target_lang,
            bot_info_client=bot_info_client,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(
                request,
                actor,
                cfg=None,
                webhook_url=_line_webhook_url_for(tenant_id),
                action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    audit_svc.record_from_actor(
        db,
        actor,
        action="line_config.upsert",
        target=f"tenant:{tenant_id}",
        detail={"by": "admin"},
        request=request,
    )
    db.commit()
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(
            request,
            actor,
            cfg=cfg,
            webhook_url=_line_webhook_url_for(tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
    )


@router.post(
    "/admin/tenants/{tenant_id}/line-config/verify", response_class=HTMLResponse
)
def admin_line_config_verify(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
    bot_info_client: LineBotInfoClient = Depends(get_bot_info_client),
):
    try:
        cfg = line_config_svc.verify_line_config(
            db, tenant_id, bot_info_client=bot_info_client
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "_line_config_status.html",
            _ctx(
                request,
                actor,
                cfg=None,
                webhook_url=_line_webhook_url_for(tenant_id),
                action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
                error=str(exc.detail),
            ),
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(
            request,
            actor,
            cfg=cfg,
            webhook_url=_line_webhook_url_for(tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
    )


@router.post(
    "/admin/tenants/{tenant_id}/line-config/delete", response_class=HTMLResponse
)
def admin_line_config_delete(
    request: Request,
    tenant_id: int,
    actor: Actor = Depends(require_ui_admin),
    db: Session = Depends(get_db),
):
    try:
        line_config_svc.delete_line_config(db, tenant_id)
        audit_svc.record_from_actor(
            db,
            actor,
            action="line_config.delete",
            target=f"tenant:{tenant_id}",
            detail={"by": "admin"},
            request=request,
        )
        db.commit()
    except HTTPException:
        pass
    return templates.TemplateResponse(
        "_line_config_status.html",
        _ctx(
            request,
            actor,
            cfg=None,
            webhook_url=_line_webhook_url_for(tenant_id),
            action_base=f"/ui/admin/tenants/{tenant_id}/line-config",
        ),
    )



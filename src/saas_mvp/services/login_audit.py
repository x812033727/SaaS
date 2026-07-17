"""登入稽核 + 異常 IP 通知(R5-D1)。

四條登入路徑統一入口(/auth/token、/ui/login、OAuth callback;console 走
/auth/token 自然涵蓋):
* 成功 → ``auth.login.success`` / ``auth.oauth.login`` 稽核 + 更新
  users.last_login_at/ip;本次 IP ≠ 上次 → email 通知(24h 冷卻)。
* 失敗 → ``auth.login.failure`` 稽核;email 只存雜湊(防列舉:稽核頁
  不得成為「哪些信箱已註冊」的查詢器)。

慣例:
* 永不拋錯 — 登入主流程絕不因稽核/通知失敗而中斷。
* 本模組自行 commit(登入路徑此刻沒有其他待提交的業務狀態)。
* 冷卻不加欄位:查 email_deliveries 既有佇列(user_id+category+created_at)。
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.email_delivery import EmailDelivery
from saas_mvp.models.user import User
from saas_mvp.services import audit
from saas_mvp.services import email_delivery as email_svc
from saas_mvp.services.mailer import Mailer, get_mailer

_log = logging.getLogger(__name__)

LOGIN_ALERT_CATEGORY = "login_alert"
LOGIN_ALERT_COOLDOWN_HOURS = 24
_TAIPEI = ZoneInfo("Asia/Taipei")


def client_ip(request) -> str | None:
    """X-Forwarded-For 首跳優先(nginx 之後),退 request.client.host。"""
    try:
        return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else None
        )
    except Exception:  # noqa: BLE001
        return None


def _user_agent(request) -> str:
    try:
        return (request.headers.get("user-agent") or "")[:200]
    except Exception:  # noqa: BLE001
        return ""


def email_digest(email: str) -> str:
    """失敗稽核只存 email 雜湊(截 16 hex),同帳號可關聯、不可反查。"""
    return hashlib.sha256((email or "").strip().lower().encode()).hexdigest()[:16]


def on_login_failure(
    db: Session,
    *,
    email: str,
    request,
    method: str = "password",
) -> None:
    """記一筆登入失敗稽核。永不拋錯。"""
    try:
        audit.record(
            db,
            action="auth.login.failure",
            detail={
                "method": method,
                "email_sha256": email_digest(email),
                "ua": _user_agent(request),
            },
            ip=client_ip(request),
        )
        db.commit()
    except Exception:  # noqa: BLE001 — 稽核永不影響登入主流程
        _log.warning("login_audit.on_login_failure failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def on_login_success(
    db: Session,
    user: User,
    request,
    *,
    method: str = "password",
    mailer: Mailer | None = None,
) -> None:
    """成功登入:稽核 + 更新 last_login + 新 IP 通知。永不拋錯。"""
    try:
        ip = client_ip(request)
        action = "auth.oauth.login" if method.startswith("oauth") else "auth.login.success"
        audit.record(
            db,
            action=action,
            actor_user_id=user.id,
            tenant_id=user.tenant_id,
            detail={"method": method, "ua": _user_agent(request)},
            ip=ip,
        )
        prev_ip = user.last_login_ip
        user.last_login_at = datetime.datetime.now(datetime.timezone.utc)
        if ip:
            user.last_login_ip = ip[:64]
        db.commit()
        if ip and prev_ip and ip != prev_ip:
            _maybe_send_alert(db, user, ip=ip, method=method, mailer=mailer)
    except Exception:  # noqa: BLE001
        _log.warning(
            "login_audit.on_login_success failed user=%s",
            getattr(user, "id", None),
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _maybe_send_alert(
    db: Session,
    user: User,
    *,
    ip: str,
    method: str,
    mailer: Mailer | None = None,
) -> None:
    """新 IP 登入 → email 通知;同 user 24h 內已通知過則跳過(防轟炸:
    家/公司雙 IP 輪替不該每次登入都寄信)。"""
    if not user.email:
        return
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=LOGIN_ALERT_COOLDOWN_HOURS
    )
    recent = (
        db.query(EmailDelivery.id)
        .filter(
            EmailDelivery.user_id == user.id,
            EmailDelivery.category == LOGIN_ALERT_CATEGORY,
            EmailDelivery.created_at >= cutoff,
        )
        .first()
    )
    if recent is not None:
        return
    when = datetime.datetime.now(_TAIPEI).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"您的帳號 {user.email} 於 {when}(台北時間)從新的 IP 位置登入。",
        f"IP:{ip}",
        f"登入方式:{'社群登入' if method.startswith('oauth') else '密碼登入'}",
        "",
        "若是您本人操作,可忽略本信。",
        "若不是您,請立即變更密碼:",
    ]
    if settings.public_base_url:
        lines.append(f"{settings.public_base_url.rstrip('/')}/ui/forgot-password")
    else:
        lines.append("請至後台「帳號設定」變更密碼。")
    m = mailer or get_mailer(db)
    email_svc.deliver_or_queue(
        db,
        m,
        user_id=user.id,
        category=LOGIN_ALERT_CATEGORY,
        recipient=user.email,
        subject="【安全通知】您的帳號從新位置登入",
        body="\n".join(lines),
    )

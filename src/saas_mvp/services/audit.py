"""稽核服務（F1）— 單一入口 ``record()``,永不拋錯。

慣例:
* **同呼叫端 session、不另 commit** — 隨業務交易一起提交;業務 rollback 時
  稽核一起消失(失敗的操作不該留「已做」軌跡)。
* detail 經敏感鍵過濾(secret/token/password 等永不落庫)。
* 失敗只 log warning,絕不影響業務主流程。

action 命名慣例(常數字串,module.entity.verb):
  admin.tenant.patch / admin.feature.set / impersonation.start / impersonation.stop
  billing.plan.subscribe / billing.plan.unsubscribe / billing.feature.subscribe /
  billing.feature.unsubscribe / line_config.upsert / line_config.delete /
  member.invite / member.join / gcal.disconnect …
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from saas_mvp.models.audit_log import AuditLog

_log = logging.getLogger(__name__)

# 敏感鍵(不分大小寫、子字串比對):值一律以 *** 取代。
_SENSITIVE_KEYS = (
    "secret", "token", "password", "hash_key", "hash_iv", "api_key",
    "authorization", "credential",
)


def _scrub(value: object, depth: int = 0) -> object:
    """遞迴過濾 dict/list 中的敏感鍵;深度上限防循環。"""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(s in str(k).lower() for s in _SENSITIVE_KEYS):
                out[k] = "***"
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value][:20]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


def record(
    db: Session,
    *,
    action: str,
    actor_user_id: int | None = None,
    impersonator_user_id: int | None = None,
    tenant_id: int | None = None,
    target: str | None = None,
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    """寫一筆稽核(db.add,不 commit;隨呼叫端交易提交)。永不拋錯。"""
    try:
        db.add(AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            impersonator_user_id=impersonator_user_id,
            action=action[:64],
            target=(target or "")[:128] or None,
            detail_json=(
                json.dumps(_scrub(detail), ensure_ascii=False, default=str)
                if detail else None
            ),
            ip=(ip or "")[:64] or None,
        ))
    except Exception:  # noqa: BLE001 — 稽核永不影響業務
        _log.warning("audit.record failed action=%s", action, exc_info=True)


def record_from_actor(
    db: Session,
    actor,
    *,
    action: str,
    target: str | None = None,
    detail: dict | None = None,
    request=None,
) -> None:
    """便利包裝:從 Actor 取 actor/impersonator/tenant,從 Request 取 ip。"""
    ip = None
    if request is not None:
        try:
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
                request.client.host if request.client else None
            )
        except Exception:  # noqa: BLE001
            ip = None
    record(
        db,
        action=action,
        actor_user_id=getattr(getattr(actor, "user", None), "id", None),
        impersonator_user_id=getattr(actor, "impersonator_user_id", None),
        tenant_id=getattr(getattr(actor, "user", None), "tenant_id", None),
        target=target,
        detail=detail,
        ip=ip,
    )

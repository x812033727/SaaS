"""AI 對話 context 組裝（A2.3）— 店家資訊 + 服務目錄 + 可約日期 + FAQ。

供 agent 的 system prompt 使用；設總長上限截斷防 token 成本放大。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

_MAX_CONTEXT_CHARS = 3000


def build_agent_context(
    db: Session, tenant_id: int, *, line_user_id: str | None = None
) -> str:
    from saas_mvp.models.tenant import Tenant
    from saas_mvp.services import catalog as catalog_svc
    from saas_mvp.services import faq as faq_svc
    from saas_mvp.services.booking_form import available_dates

    parts: list[str] = []

    tenant = db.get(Tenant, tenant_id)
    if tenant is not None:
        parts.append(f"店名：{tenant.name}")

    services = [
        s for s in catalog_svc.list_services(db, tenant_id=tenant_id) if s.is_active
    ][:20]
    if services:
        lines = []
        for s in services:
            bits = [f"id={s.id}", s.name]
            if s.duration_minutes:
                bits.append(f"{s.duration_minutes}分鐘")
            if s.price_cents:
                bits.append(f"NT${s.price_cents // 100}")
            lines.append("・" + " ".join(bits))
        parts.append("服務項目：\n" + "\n".join(lines))

    dates = available_dates(db, tenant_id, limit=7)
    if dates:
        parts.append("近期可預約日期：" + "、".join(dates))

    # D1:顧客現有預約(改期/取消意圖需要對出正確編號)。
    if line_user_id:
        from saas_mvp.services import booking as booking_svc

        mine = booking_svc.list_my_reservations(
            db, tenant_id=tenant_id, line_user_id=line_user_id
        )
        if mine:
            parts.append(
                "顧客現有預約:\n" + "\n".join(
                    f"#{r.id} {r.party_size} 位" for r in mine[:10]
                )
            )

    try:
        faq_ctx = faq_svc.build_context(db, tenant_id, "", max_entries=3)
    except Exception:  # noqa: BLE001 — FAQ 失敗不阻擋
        faq_ctx = ""
    if faq_ctx:
        parts.append(faq_ctx)

    return "\n\n".join(parts)[:_MAX_CONTEXT_CHARS]

"""成員管理(R5-D3)— owner 對同租戶成員的停用/啟用/角色切換/移除。

授權模型:``User.role``(owner/staff)是 /ui 面唯一權威(見 require_ui_owner)。
角色變更同步 TenantMember/OrganizationMember(比照 organizations.ensure_user_memberships
的推導),讓 /v1 API 的權限也一致。

不變量:租戶**至少保留一位啟用中的 owner**。停用/降級/移除「最後一位啟用 owner」
一律擋下。所有變動由呼叫端(router)記 audit;本模組只做狀態變更 + commit。
"""

from __future__ import annotations

import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_mvp.models.organization import OrganizationMember, TenantMember
from saas_mvp.models.plan_change_history import PlanChangeHistory
from saas_mvp.models.user import User


class MemberActionError(ValueError):
    """成員操作被規則擋下(使用者可讀訊息)。"""


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def list_members(db: Session, tenant_id: int) -> list[User]:
    return (
        db.query(User)
        .filter(User.tenant_id == tenant_id)
        .order_by(User.id)
        .all()
    )


def active_owner_count(db: Session, tenant_id: int) -> int:
    """啟用中(未停用)的 owner 數。"""
    return (
        db.query(User)
        .filter(
            User.tenant_id == tenant_id,
            User.role == "owner",
            User.disabled_at.is_(None),
        )
        .count()
    )


def _target(db: Session, actor_user: User, target_id: int) -> User:
    target = db.get(User, target_id)
    if target is None or target.tenant_id != actor_user.tenant_id:
        raise MemberActionError("找不到該成員。")
    if target.id == actor_user.id:
        raise MemberActionError("無法對自己執行此操作。")
    return target


def _sync_membership_roles(db: Session, target: User) -> None:
    """角色改動後,同步 tenant/org membership 的 role(比照 ensure_user_memberships)。"""
    tenant_role = target.role if target.role in ("owner", "staff") else "viewer"
    org_role = "owner" if tenant_role == "owner" else "viewer"
    tm = db.execute(
        select(TenantMember).where(TenantMember.user_id == target.id)
    ).scalars().all()
    for m in tm:
        m.role = tenant_role
    om = db.execute(
        select(OrganizationMember).where(OrganizationMember.user_id == target.id)
    ).scalars().all()
    for m in om:
        m.role = org_role


def disable_member(db: Session, actor_user: User, target_id: int) -> User:
    """停用成員:設 disabled_at + token_version+1(切斷既有票/API key)。"""
    target = _target(db, actor_user, target_id)
    if target.disabled_at is not None:
        return target
    if (
        target.role == "owner"
        and active_owner_count(db, actor_user.tenant_id) <= 1
    ):
        raise MemberActionError("至少需保留一位啟用中的負責人,無法停用。")
    target.disabled_at = _utcnow()
    target.token_version = (target.token_version or 0) + 1
    db.commit()
    return target


def enable_member(db: Session, actor_user: User, target_id: int) -> User:
    target = _target(db, actor_user, target_id)
    target.disabled_at = None
    db.commit()
    return target


def set_role(db: Session, actor_user: User, target_id: int, role: str) -> User:
    """切換 owner↔staff。降級最後一位啟用 owner → 擋。角色即時生效(每請求重載)。"""
    if role not in ("owner", "staff"):
        raise MemberActionError("角色僅接受 owner 或 staff。")
    target = _target(db, actor_user, target_id)
    if target.role == role:
        return target
    if (
        target.role == "owner"
        and role != "owner"
        and active_owner_count(db, actor_user.tenant_id) <= 1
    ):
        raise MemberActionError("至少需保留一位啟用中的負責人,無法降級。")
    target.role = role
    _sync_membership_roles(db, target)
    db.commit()
    return target


def remove_member(db: Session, actor_user: User, target_id: int) -> None:
    """移除成員(硬刪,memberships cascade)。最後一位啟用 owner → 擋。

    登入 User 無硬性薪資外鍵(commission.created_by_user_id 為純 Integer 稽核欄,
    刪除不破壞);audit_logs.actor_user_id 為 SET NULL。
    """
    target = _target(db, actor_user, target_id)
    if (
        target.role == "owner"
        and target.disabled_at is None
        and active_owner_count(db, actor_user.tenant_id) <= 1
    ):
        raise MemberActionError("至少需保留一位啟用中的負責人,無法移除。")
    # plan_change_history.changed_by_user_id 是 RESTRICT FK(無 ondelete),不先
    # 匿名化會讓刪除拋 IntegrityError→500。比照 sibling 表 SET NULL 語意保留紀錄。
    db.execute(
        update(PlanChangeHistory)
        .where(PlanChangeHistory.changed_by_user_id == target.id)
        .values(changed_by_user_id=None)
    )
    db.delete(target)
    try:
        db.commit()
    except IntegrityError:
        # backstop:任何其他未預期的 RESTRICT 關聯 → 給乾淨錯誤而非 500;
        # owner 可改用「停用」達到切斷存取的效果。
        db.rollback()
        raise MemberActionError(
            "此成員仍有關聯資料無法直接移除,請改用「停用」。"
        )


def logout_all_devices(db: Session, user: User) -> User:
    """登出所有裝置:token_version+1 撤銷所有既有票(呼叫端負責重簽本裝置 cookie)。"""
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    return user

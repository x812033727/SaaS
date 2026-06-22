"""建立或提權「平台管理員」帳號（設定 User.is_admin）。

平台管理端點（``/admin/*``、後台 ``/ui/admin/bots``、``/ui/admin/tenants/{id}``）
以 ``User.is_admin`` 為閘門。``/auth/register`` 刻意不開放設定此旗標（防自助提權），
故管理員必須由具 DB 權限者用本腳本設定。

Usage:
    # 提權既有帳號為管理員
    python -m saas_mvp.ops.promote_admin --email owner@shop.tw

    # 一次建立全新的專屬管理員帳號（含其租戶）
    python -m saas_mvp.ops.promote_admin --email admin@you.tw --password 'S3cret!!' --create

    # 取消某帳號的管理員權限
    python -m saas_mvp.ops.promote_admin --email owner@shop.tw --demote

設計（比照其它 ops）：argparse + 可注入 session_factory（供測試）；
standalone 執行先 ``import_all_models()`` 確保 ORM relationship 字串可解析。
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, TextIO

from sqlalchemy.orm import Session, sessionmaker

from saas_mvp.auth.security import hash_password
from saas_mvp.db import SessionLocal, import_all_models
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User


class PromoteError(Exception):
    """可預期的使用錯誤（回非零退出碼，不印 traceback）。"""


def promote_admin(
    session: Session,
    *,
    email: str,
    create: bool = False,
    password: str | None = None,
    tenant_name: str | None = None,
    demote: bool = False,
) -> tuple[str, User]:
    """設定 / 取消管理員。回傳 (action, user)。在單一 session 內完成並 commit。"""
    email = email.strip().lower()
    existing = session.query(User).filter(User.email == email).first()

    if create:
        if demote:
            raise PromoteError("--create 與 --demote 不可併用")
        if not password:
            raise PromoteError("--create 需同時提供 --password")
        if existing:
            raise PromoteError(
                f"email 已存在：{email}（請改用提權模式，去掉 --create）"
            )
        tname = (tenant_name or f"admin-{email}").strip()
        if session.query(Tenant).filter(Tenant.name == tname).first():
            raise PromoteError(f"租戶名稱已被使用：{tname}（用 --tenant-name 指定別的）")
        tenant = Tenant(name=tname, plan="free")
        session.add(tenant)
        session.flush()  # 取得 tenant.id
        user = User(
            email=email,
            hashed_password=hash_password(password),
            tenant_id=tenant.id,
            is_admin=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return ("created", user)

    if not existing:
        raise PromoteError(
            f"查無帳號：{email}（要新建請加 --create --password ...）"
        )

    target = not demote  # demote → False；否則 True
    if existing.is_admin == target:
        action = "already-admin" if target else "already-regular"
        return (action, existing)

    existing.is_admin = target
    session.commit()
    session.refresh(existing)
    return ("promoted" if target else "demoted", existing)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m saas_mvp.ops.promote_admin",
        description="建立或提權平台管理員（User.is_admin）。",
    )
    p.add_argument("--email", required=True, help="目標帳號 email")
    p.add_argument("--create", action="store_true",
                   help="建立全新管理員帳號（含其租戶）；需 --password")
    p.add_argument("--password", help="--create 時的新帳號密碼")
    p.add_argument("--tenant-name", help="--create 時的租戶名稱（預設 admin-<email>）")
    p.add_argument("--demote", action="store_true", help="取消管理員權限")
    return p


def main(argv: list[str] | None = None, *,
         session_factory: Callable[[], Session] | sessionmaker | None = None,
         out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    # standalone（cron / shell）執行時，registry 未必完整 → 先註冊所有 model，
    # 否則 relationship 字串（如 'Tenant'）解析失敗。
    import_all_models()
    out = out or sys.stdout
    factory = session_factory or SessionLocal

    session = factory()
    try:
        action, user = promote_admin(
            session,
            email=args.email,
            create=args.create,
            password=args.password,
            tenant_name=args.tenant_name,
            demote=args.demote,
        )
    except PromoteError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    finally:
        session.close()

    msg = {
        "created": "已建立管理員帳號",
        "promoted": "已提權為管理員",
        "demoted": "已取消管理員權限",
        "already-admin": "帳號已是管理員（無變更）",
        "already-regular": "帳號本就非管理員（無變更）",
    }[action]
    print(f"{msg}：id={user.id} email={user.email} "
          f"tenant_id={user.tenant_id} is_admin={user.is_admin}", file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

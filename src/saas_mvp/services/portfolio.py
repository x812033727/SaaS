"""作品集（portfolio）服務層 — 分類 + 作品 CRUD + 公開列表。

所有查詢走 tenant_query 強制隔離；查無/跨租戶一律 404，不洩漏 ID 存在性。
list_public 回啟用中作品、依 sort_order 排序（公開店家頁作品集分頁用）。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from saas_mvp.models.portfolio_category import PortfolioCategory
from saas_mvp.models.portfolio_item import PortfolioItem
from saas_mvp.services.tenants import tenant_query


# ── 分類 ──────────────────────────────────────────────────────────────────────

def _get_category_or_404(
    db: Session, tenant_id: int, category_id: int
) -> PortfolioCategory:
    cat = (
        tenant_query(db, PortfolioCategory, tenant_id)
        .filter(PortfolioCategory.id == category_id)
        .first()
    )
    if cat is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio category not found"
        )
    return cat


def list_categories(db: Session, *, tenant_id: int) -> list[PortfolioCategory]:
    return (
        tenant_query(db, PortfolioCategory, tenant_id)
        .order_by(PortfolioCategory.sort_order, PortfolioCategory.id)
        .all()
    )


def get_category(
    db: Session, *, tenant_id: int, category_id: int
) -> PortfolioCategory:
    return _get_category_or_404(db, tenant_id, category_id)


def create_category(
    db: Session, *, tenant_id: int, name: str, sort_order: int = 0
) -> PortfolioCategory:
    cat = PortfolioCategory(tenant_id=tenant_id, name=name, sort_order=sort_order)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def update_category(
    db: Session,
    *,
    tenant_id: int,
    category_id: int,
    name: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> PortfolioCategory:
    cat = _get_category_or_404(db, tenant_id, category_id)
    if name is not None:
        cat.name = name
    if sort_order is not None:
        cat.sort_order = sort_order
    if is_active is not None:
        cat.is_active = is_active
    db.commit()
    db.refresh(cat)
    return cat


def delete_category(db: Session, *, tenant_id: int, category_id: int) -> None:
    cat = _get_category_or_404(db, tenant_id, category_id)
    db.delete(cat)
    db.commit()


# ── 作品 ──────────────────────────────────────────────────────────────────────

def _get_item_or_404(db: Session, tenant_id: int, item_id: int) -> PortfolioItem:
    item = (
        tenant_query(db, PortfolioItem, tenant_id)
        .filter(PortfolioItem.id == item_id)
        .first()
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio item not found"
        )
    return item


def list_items(
    db: Session, *, tenant_id: int, category_id: int | None = None
) -> list[PortfolioItem]:
    q = tenant_query(db, PortfolioItem, tenant_id)
    if category_id is not None:
        q = q.filter(PortfolioItem.category_id == category_id)
    return q.order_by(PortfolioItem.sort_order, PortfolioItem.id).all()


def get_item(db: Session, *, tenant_id: int, item_id: int) -> PortfolioItem:
    return _get_item_or_404(db, tenant_id, item_id)


def create_item(
    db: Session,
    *,
    tenant_id: int,
    image_url: str,
    category_id: int | None = None,
    caption: str | None = None,
    sort_order: int = 0,
) -> PortfolioItem:
    item = PortfolioItem(
        tenant_id=tenant_id,
        image_url=image_url,
        category_id=category_id,
        caption=caption,
        sort_order=sort_order,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_item(
    db: Session,
    *,
    tenant_id: int,
    item_id: int,
    image_url: str | None = None,
    category_id: int | None = None,
    caption: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> PortfolioItem:
    item = _get_item_or_404(db, tenant_id, item_id)
    if image_url is not None:
        item.image_url = image_url
    if category_id is not None:
        item.category_id = category_id
    if caption is not None:
        item.caption = caption
    if sort_order is not None:
        item.sort_order = sort_order
    if is_active is not None:
        item.is_active = is_active
    db.commit()
    db.refresh(item)
    return item


def delete_item(db: Session, *, tenant_id: int, item_id: int) -> None:
    item = _get_item_or_404(db, tenant_id, item_id)
    db.delete(item)
    db.commit()


def list_public(db: Session, tenant_id: int) -> list[PortfolioItem]:
    """公開店家頁用：該租戶啟用中的作品，依 sort_order 排序。"""
    return (
        tenant_query(db, PortfolioItem, tenant_id)
        .filter(PortfolioItem.is_active.is_(True))
        .order_by(PortfolioItem.sort_order, PortfolioItem.id)
        .all()
    )

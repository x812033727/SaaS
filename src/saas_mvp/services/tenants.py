"""Tenant 服務層 — 租戶範圍查詢與隔離保護。

核心設計：
- `tenant_query()` 是所有業務查詢的唯一入口；直接 db.query(Model) 是禁止的。
- `require_same_tenant()` 作為最後一道防線，處理無法用 filter 表達的場景。
"""

from __future__ import annotations

from typing import Type, TypeVar

from fastapi import HTTPException, status
from sqlalchemy.orm import Query, Session

# 使用字串型別避免循環 import；Base 本身不帶 tenant_id，用 Protocol 描述
_T = TypeVar("_T")


def tenant_query(db: Session, model: Type[_T], tenant_id: int) -> Query:
    """回傳已套用 tenant_id filter 的 Query。

    所有 Service 函式必須透過這個 helper，絕不直接 db.query(Model)，
    以確保查詢層強制隔離、不會因為遺漏 filter 而靜默洩漏跨租戶資料。

    用法::

        notes = tenant_query(db, Note, current_user.tenant_id).all()
        note  = tenant_query(db, Note, tid).filter(Note.id == note_id).first()
    """
    return db.query(model).filter(model.tenant_id == tenant_id)  # type: ignore[attr-defined]


def require_same_tenant(resource_tenant_id: int, current_tenant_id: int) -> None:
    """確認資源屬於當前租戶，否則拋 403。

    作為補充防線使用（例如：從 relationship 取得的物件無法套 filter）。
    主路徑優先用 tenant_query()，不需要再呼叫此函式。
    """
    if resource_tenant_id != current_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: resource belongs to a different tenant",
        )

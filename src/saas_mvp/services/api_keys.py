"""API key 管理服務層 — 建立（明文只回傳一次）/ 列出 / 撤銷。

供 REST router 與 UI 共用。DB 只存 key_prefix + sha256 hash，
明文 key 只在 create_key 的回傳值出現一次，之後永遠無法再取得。
"""

from __future__ import annotations

import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.api_key import (
    ApiKey,
    generate_api_key,
    get_key_prefix,
    hash_api_key,
)


def create_key(
    db: Session, *, tenant_id: int, user_id: int, name: str
) -> tuple[ApiKey, str]:
    """建立 API key；回傳 (row, 明文 key)。明文僅此一次。"""
    plain_key = generate_api_key()
    api_key = ApiKey(
        user_id=user_id,
        tenant_id=tenant_id,
        name=name,
        key_prefix=get_key_prefix(plain_key),
        key_hash=hash_api_key(plain_key),
        is_active=True,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return api_key, plain_key


def list_keys(db: Session, *, tenant_id: int) -> list[ApiKey]:
    return list(
        db.execute(
            select(ApiKey).where(ApiKey.tenant_id == tenant_id)
        ).scalars().all()
    )


def revoke_key(db: Session, *, tenant_id: int, key_id: int) -> None:
    """撤銷（軟刪除，is_active=False）；usage 歷史保留、撤銷後立即失效。"""
    row = db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
        )
    row.is_active = False
    db.commit()

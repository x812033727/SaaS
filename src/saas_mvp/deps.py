"""統一 FastAPI 依賴入口。

所有 router 一律從此處 import，不要直接跨模組拉 auth.dependencies 或 db。
"""

from fastapi import Depends, HTTPException, status

from saas_mvp.auth.dependencies import Actor as Actor  # noqa: F401
from saas_mvp.auth.dependencies import get_current_actor as get_current_actor  # noqa: F401
from saas_mvp.auth.dependencies import get_current_user as get_current_user  # noqa: F401
from saas_mvp.db import get_db as get_db  # noqa: F401
from saas_mvp.db import get_session_factory as get_session_factory  # noqa: F401
from saas_mvp.quota import require_quota as require_quota  # noqa: F401
from saas_mvp.auth.ratelimit import require_rate_limit as require_rate_limit  # noqa: F401


def require_admin(actor: Actor = Depends(get_current_actor)) -> Actor:
    """非 admin 一律回 403（不回 401，避免洩漏端點存在）。"""
    if not actor.user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin required",
        )
    return actor

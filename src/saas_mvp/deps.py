"""統一 FastAPI 依賴入口。

所有 router 一律從此處 import，不要直接跨模組拉 auth.dependencies 或 db。
"""

from saas_mvp.auth.dependencies import Actor as Actor  # noqa: F401
from saas_mvp.auth.dependencies import get_current_actor as get_current_actor  # noqa: F401
from saas_mvp.auth.dependencies import get_current_user as get_current_user  # noqa: F401
from saas_mvp.db import get_db as get_db  # noqa: F401
from saas_mvp.quota import require_quota as require_quota  # noqa: F401

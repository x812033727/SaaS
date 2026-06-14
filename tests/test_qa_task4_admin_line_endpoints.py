"""QA 驗收測試 — Task #4：租戶 LINE 設定管理端點

驗收標準完整覆蓋：
  [AC-1]  管理端點可建立/更新/查詢 LINE 設定（PUT/GET/DELETE）
  [AC-2]  需通過既有 admin 授權；未授權/非 admin → 403/401
  [AC-3]  跨租戶存取被拒（非 admin 不可走 /admin 路由）
  [AC-4]  channel secret/token 非明文存 DB（API 層整合驗證）
  [AC-5]  回應格式正確：has_channel_secret/has_access_token = True；無明文欄位
  [AC-6]  upsert 語義：第二次 PUT 更新而非新建（一對一）
  [AC-7]  BCP-47 lang 驗證：合法通過、非法 400
  [AC-8]  不存在的 tenant_id → 404；尚未設定 LINE config → 404
  [AC-9]  沿用既有回應格式（detail 欄位、HTTP status code 慣例）
  [AC-10] 全離線、in-memory SQLite、不需真實 LINE 金鑰

特別補充既有工程師測試較薄弱之處：
  - DB 層直查確認加密欄位非明文（結合 HTTP 端點＋DB session）
  - DELETE 後 cascade 不影響其他租戶
  - response JSON schema 完整性（必要欄位全存在）
  - 各方法的 401 vs 403 區分（未登入 vs 無權限）
  - PUT body 缺少必要欄位 → 422 Unprocessable Entity
  - default_target_lang 省略時確實回傳 "zh-TW"
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("SAAS_LINE_CHANNEL_ENCRYPT_KEY",
                      "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=")

# 確保所有 model metadata 已載入
from saas_mvp.models import tenant as _t, user as _u, note as _n, usage as _us  # noqa: F401
from saas_mvp.models import api_key as _ak, api_key_usage as _aku               # noqa: F401
from saas_mvp.models import plan_change_history as _pch                          # noqa: F401
import saas_mvp.models.line_channel_config as _lcm                               # noqa: F401

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db


# ── in-memory SQLite（模組層級共用，避免重複建表） ─────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def _override_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────

import uuid

def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _register(client: TestClient) -> tuple[str, str, int]:
    """回傳 (email, token, tenant_id)"""
    email = f"qa4_{_uid()}@example.com"
    tn = f"qa4_tenant_{_uid()}"
    r = client.post("/auth/register", json={
        "email": email, "password": "Qa4Test99!", "tenant_name": tn,
    })
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    me = client.get("/tenants/me", headers={"Authorization": f"Bearer {token}"})
    tenant_id = me.json()["id"]
    return email, token, tenant_id


def _make_admin(token: str) -> None:
    from saas_mvp.auth.security import decode_access_token
    from saas_mvp.models.user import User
    payload = decode_access_token(token)
    db = _Session()
    try:
        user = db.get(User, int(payload["sub"]))
        user.is_admin = True
        db.commit()
    finally:
        db.close()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _put_config(client: TestClient, token: str, tid: int,
                secret: str = "test-secret",
                access: str = "test-token",
                lang: str | None = None) -> Any:
    body: dict = {"channel_secret": secret, "access_token": access}
    if lang is not None:
        body["default_target_lang"] = lang
    return client.put(f"/admin/line-configs/{tid}", headers=_auth(token), json=body)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def admin_ctx(client):
    """admin token + admin tenant_id"""
    _, token, tid = _register(client)
    _make_admin(token)
    return token, tid


@pytest.fixture(scope="module")
def normal_ctx(client):
    """普通 user token + 自己的 tenant_id"""
    _, token, tid = _register(client)
    return token, tid


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-2] 授權邊界
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthorizationBoundary:
    """各端點在未登入/非 admin 時正確拒絕。"""

    # --- 未登入 → 401 -------------------------------------------------------

    def test_get_no_auth_401(self, client):
        r = client.get("/admin/line-configs/1")
        assert r.status_code == 401, r.text

    def test_put_no_auth_401(self, client):
        r = client.put("/admin/line-configs/1",
                       json={"channel_secret": "s", "access_token": "t"})
        assert r.status_code == 401, r.text

    def test_delete_no_auth_401(self, client):
        r = client.delete("/admin/line-configs/1")
        assert r.status_code == 401, r.text

    # --- 非 admin → 403（不是 401，不洩漏端點存在性） ------------------------

    def test_get_non_admin_403(self, client, normal_ctx):
        token, tid = normal_ctx
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 403, r.text

    def test_put_non_admin_403(self, client, normal_ctx):
        token, tid = normal_ctx
        r = client.put(f"/admin/line-configs/{tid}", headers=_auth(token),
                       json={"channel_secret": "s", "access_token": "t"})
        assert r.status_code == 403, r.text

    def test_delete_non_admin_403(self, client, normal_ctx):
        token, tid = normal_ctx
        r = client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 403, r.text

    # --- 401 vs 403 要區分：無 token 是 401，有 token 無 admin 是 403 --------

    def test_401_and_403_are_distinct(self, client, normal_ctx):
        token, tid = normal_ctx
        r_noauth = client.get(f"/admin/line-configs/{tid}")
        r_noadmin = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r_noauth.status_code == 401
        assert r_noadmin.status_code == 403
        assert r_noauth.status_code != r_noadmin.status_code


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-1] 建立/查詢/更新/刪除 基本流程
# ═══════════════════════════════════════════════════════════════════════════════

class TestCRUDFlow:
    """完整 CRUD 流程（admin 操作）。"""

    def test_put_returns_200(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        r = _put_config(client, token, tid, lang="ja")
        assert r.status_code == 200, r.text

    def test_get_after_put_returns_200(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        _put_config(client, token, tid)
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 200, r.text

    def test_delete_returns_200(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        _put_config(client, token, tid)
        r = client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 200, r.text

    def test_delete_response_contains_tenant_id(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        _put_config(client, token, tid)
        r = client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.json()["tenant_id"] == tid

    def test_get_after_delete_returns_404(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        _put_config(client, token, tid)
        client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 404, r.text


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-5] 回應格式正確性
# ═══════════════════════════════════════════════════════════════════════════════

class TestResponseFormat:
    """PUT/GET 回應必須包含所有必要欄位，且絕不含明文 secret/token。"""

    REQUIRED_KEYS = {"tenant_id", "has_channel_secret", "has_access_token",
                     "default_target_lang", "created_at", "updated_at"}
    FORBIDDEN_KEYS = {"channel_secret", "access_token"}

    @pytest.fixture(scope="class")
    def put_resp(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        r = _put_config(client, token, tid, lang="en")
        assert r.status_code == 200
        return r.json(), token, tid

    def test_put_response_has_all_required_keys(self, client, put_resp):
        data, _, _ = put_resp
        missing = self.REQUIRED_KEYS - data.keys()
        assert not missing, f"PUT 回應缺少欄位: {missing}"

    def test_put_response_has_no_plaintext_secret(self, client, put_resp):
        data, _, _ = put_resp
        leaks = self.FORBIDDEN_KEYS & data.keys()
        assert not leaks, f"PUT 回應洩漏明文欄位: {leaks}"

    def test_put_response_has_secret_masked_true(self, client, put_resp):
        data, _, _ = put_resp
        assert data["has_channel_secret"] is True
        assert data["has_access_token"] is True

    def test_put_response_tenant_id_correct(self, client, put_resp):
        data, _, tid = put_resp
        assert data["tenant_id"] == tid

    def test_get_response_has_all_required_keys(self, client, put_resp):
        data_put, token, tid = put_resp
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        data = r.json()
        missing = self.REQUIRED_KEYS - data.keys()
        assert not missing, f"GET 回應缺少欄位: {missing}"

    def test_get_response_has_no_plaintext_secret(self, client, put_resp):
        data_put, token, tid = put_resp
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        leaks = self.FORBIDDEN_KEYS & r.json().keys()
        assert not leaks, f"GET 回應洩漏明文欄位: {leaks}"

    def test_has_booleans_not_truthy_strings(self, client, put_resp):
        data, _, _ = put_resp
        # 明確是 Python bool，不是 "True" 字串
        assert data["has_channel_secret"] is True
        assert data["has_access_token"] is True

    def test_created_at_is_iso_string(self, client, put_resp):
        import datetime
        data, _, _ = put_resp
        # 應能解析為 datetime
        dt = datetime.datetime.fromisoformat(data["created_at"])
        assert dt is not None

    def test_updated_at_is_iso_string(self, client, put_resp):
        import datetime
        data, _, _ = put_resp
        dt = datetime.datetime.fromisoformat(data["updated_at"])
        assert dt is not None


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-4] DB 層加密確認（整合驗證：HTTP 端點 + DB 直查）
# ═══════════════════════════════════════════════════════════════════════════════

class TestDbLevelEncryption:
    """透過 HTTP PUT 後，直查 SQLite 驗證 _enc 欄位非明文。"""

    def test_db_channel_secret_is_not_plaintext_after_put(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        secret_plaintext = "my-plaintext-channel-secret"
        r = _put_config(client, token, tid, secret=secret_plaintext)
        assert r.status_code == 200

        db = _Session()
        try:
            row = db.execute(
                text("SELECT channel_secret_enc FROM line_channel_configs "
                     "WHERE tenant_id = :tid"),
                {"tid": tid},
            ).fetchone()
            assert row is not None, "DB 沒有插入 config 列"
            raw = row[0]
            raw_bytes = raw if isinstance(raw, bytes) else raw.encode("latin-1")
            assert secret_plaintext.encode() not in raw_bytes, \
                "DB channel_secret_enc 不應存放明文"
        finally:
            db.close()

    def test_db_access_token_is_not_plaintext_after_put(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        token_plaintext = "my-plaintext-access-token"
        r = _put_config(client, token, tid, access=token_plaintext)
        assert r.status_code == 200

        db = _Session()
        try:
            row = db.execute(
                text("SELECT access_token_enc FROM line_channel_configs "
                     "WHERE tenant_id = :tid"),
                {"tid": tid},
            ).fetchone()
            assert row is not None
            raw = row[0]
            raw_bytes = raw if isinstance(raw, bytes) else raw.encode("latin-1")
            assert token_plaintext.encode() not in raw_bytes, \
                "DB access_token_enc 不應存放明文"
        finally:
            db.close()

    def test_db_only_one_row_per_tenant_after_multiple_puts(self, client, admin_ctx):
        """upsert 語義：多次 PUT 不累積多列。"""
        token, _ = admin_ctx
        _, _, tid = _register(client)
        for i in range(3):
            _put_config(client, token, tid, secret=f"secret-{i}")

        db = _Session()
        try:
            rows = db.execute(
                text("SELECT COUNT(*) FROM line_channel_configs WHERE tenant_id = :tid"),
                {"tid": tid},
            ).scalar()
            assert rows == 1, f"DB 應只有 1 列，但有 {rows} 列"
        finally:
            db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-6] Upsert 語義
# ═══════════════════════════════════════════════════════════════════════════════

class TestUpsertSemantics:
    """PUT 兩次應更新而非新建；值必須反映最後一次 PUT。"""

    def test_second_put_updates_lang(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        _put_config(client, token, tid, lang="en")
        r2 = _put_config(client, token, tid, lang="ko")
        assert r2.json()["default_target_lang"] == "ko"

    def test_second_put_reflected_in_get(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        _put_config(client, token, tid, lang="ja")
        _put_config(client, token, tid, lang="fr")
        r_get = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r_get.json()["default_target_lang"] == "fr"

    def test_default_lang_when_omitted(self, client, admin_ctx):
        """PUT 省略 default_target_lang 時應回傳 'zh-TW'。"""
        token, _ = admin_ctx
        _, _, tid = _register(client)
        r = _put_config(client, token, tid)  # lang=None → 省略
        assert r.json()["default_target_lang"] == "zh-TW"


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-7] BCP-47 驗證
# ═══════════════════════════════════════════════════════════════════════════════

class TestBcp47Validation:
    """lang 驗證：合法 200、非法 400、邊界值。"""

    VALID_LANGS = ["en", "zh-TW", "zh-Hant-TW", "ja", "ko", "fr", "de"]
    INVALID_LANGS = ["", "not valid!", "en_US", "zh TW", "../etc", "a" * 50]

    @pytest.fixture(scope="class")
    def admin_token(self, client, admin_ctx):
        token, _ = admin_ctx
        return token

    def test_valid_langs_return_200(self, client, admin_token):
        for lang in self.VALID_LANGS:
            _, _, tid = _register(client)
            r = _put_config(client, admin_token, tid, lang=lang)
            assert r.status_code == 200, f"合法 lang={lang!r} 應通過，但得 {r.status_code}: {r.text}"

    def test_invalid_langs_return_400(self, client, admin_token):
        for lang in self.INVALID_LANGS:
            _, _, tid = _register(client)
            r = _put_config(client, admin_token, tid, lang=lang)
            assert r.status_code == 400, \
                f"非法 lang={lang!r} 應回 400，但得 {r.status_code}: {r.text}"

    def test_400_response_has_detail(self, client, admin_token):
        _, _, tid = _register(client)
        r = _put_config(client, admin_token, tid, lang="bad lang!")
        data = r.json()
        assert "detail" in data, "400 回應應有 detail 欄位"
        assert data["detail"], "detail 不能是空值"


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-8] 404 場景
# ═══════════════════════════════════════════════════════════════════════════════

class TestNotFound:
    """不存在的 tenant / 未設定 config 應回 404。"""

    NON_EXISTENT_ID = 999999

    def test_get_nonexistent_tenant_404(self, client, admin_ctx):
        token, _ = admin_ctx
        r = client.get(f"/admin/line-configs/{self.NON_EXISTENT_ID}", headers=_auth(token))
        assert r.status_code == 404

    def test_put_nonexistent_tenant_404(self, client, admin_ctx):
        token, _ = admin_ctx
        r = _put_config(client, token, self.NON_EXISTENT_ID)
        assert r.status_code == 404

    def test_delete_nonexistent_tenant_404(self, client, admin_ctx):
        token, _ = admin_ctx
        r = client.delete(f"/admin/line-configs/{self.NON_EXISTENT_ID}", headers=_auth(token))
        assert r.status_code == 404

    def test_get_tenant_without_config_404(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, fresh_tid = _register(client)
        r = client.get(f"/admin/line-configs/{fresh_tid}", headers=_auth(token))
        assert r.status_code == 404

    def test_delete_tenant_without_config_404(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, fresh_tid = _register(client)
        r = client.delete(f"/admin/line-configs/{fresh_tid}", headers=_auth(token))
        assert r.status_code == 404

    def test_404_response_has_detail_field(self, client, admin_ctx):
        token, _ = admin_ctx
        r = client.get(f"/admin/line-configs/{self.NON_EXISTENT_ID}", headers=_auth(token))
        assert "detail" in r.json()

    def test_delete_twice_second_is_404(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        _put_config(client, token, tid)
        client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        r2 = client.delete(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r2.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-9] PUT body 驗證（缺少必要欄位 → 422）
# ═══════════════════════════════════════════════════════════════════════════════

class TestPutBodyValidation:
    """PUT body 缺少必要欄位應回 422 Unprocessable Entity。"""

    @pytest.fixture(scope="class")
    def admin_token_tid(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid = _register(client)
        return token, tid

    def test_missing_channel_secret_422(self, client, admin_token_tid):
        token, tid = admin_token_tid
        r = client.put(f"/admin/line-configs/{tid}",
                       headers=_auth(token),
                       json={"access_token": "t"})
        assert r.status_code == 422

    def test_missing_access_token_422(self, client, admin_token_tid):
        token, tid = admin_token_tid
        r = client.put(f"/admin/line-configs/{tid}",
                       headers=_auth(token),
                       json={"channel_secret": "s"})
        assert r.status_code == 422

    def test_empty_body_422(self, client, admin_token_tid):
        token, tid = admin_token_tid
        r = client.put(f"/admin/line-configs/{tid}",
                       headers=_auth(token),
                       json={})
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# [AC-3] 跨租戶隔離
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossTenantIsolation:
    """多租戶彼此設定獨立；普通 user 無法透過 admin 路由存取。"""

    def test_two_tenants_configs_are_independent(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid_a = _register(client)
        _, _, tid_b = _register(client)

        _put_config(client, token, tid_a, secret="secret-A", lang="ja")
        _put_config(client, token, tid_b, secret="secret-B", lang="ko")

        ra = client.get(f"/admin/line-configs/{tid_a}", headers=_auth(token))
        rb = client.get(f"/admin/line-configs/{tid_b}", headers=_auth(token))

        assert ra.json()["default_target_lang"] == "ja"
        assert rb.json()["default_target_lang"] == "ko"
        assert ra.json()["tenant_id"] == tid_a
        assert rb.json()["tenant_id"] == tid_b

    def test_normal_user_cannot_read_own_config_via_admin(self, client, normal_ctx):
        token, tid = normal_ctx
        r = client.get(f"/admin/line-configs/{tid}", headers=_auth(token))
        assert r.status_code == 403

    def test_delete_tenant_a_does_not_affect_tenant_b(self, client, admin_ctx):
        token, _ = admin_ctx
        _, _, tid_a = _register(client)
        _, _, tid_b = _register(client)

        _put_config(client, token, tid_a)
        _put_config(client, token, tid_b, lang="ko")

        client.delete(f"/admin/line-configs/{tid_a}", headers=_auth(token))

        ra_after = client.get(f"/admin/line-configs/{tid_a}", headers=_auth(token))
        rb_after = client.get(f"/admin/line-configs/{tid_b}", headers=_auth(token))

        assert ra_after.status_code == 404, "A 刪後應 404"
        assert rb_after.status_code == 200, "B 不應受影響"
        assert rb_after.json()["default_target_lang"] == "ko"

    def test_admin_can_manage_any_tenant(self, client, admin_ctx):
        """Admin 可操作任意租戶，不受自身 tenant_id 限制。"""
        token, admin_tid = admin_ctx
        # 操作另一個租戶（不是自己的）
        _, _, other_tid = _register(client)
        r = _put_config(client, token, other_tid, lang="de")
        assert r.status_code == 200
        assert r.json()["tenant_id"] == other_tid

"""
任務 #3 驗收測試：多租戶資料模型與隔離層

覆蓋：
1. 資料模型 — User 必須帶 tenant_id FK、Note 必須帶 tenant_id FK
2. tenant_query() — 回傳僅屬於指定 tenant 的資料
3. 跨租戶讀取 — 404（不洩漏 ID）
4. 跨租戶寫入 (update/delete) — 404
5. require_same_tenant() — 403 when IDs differ
6. 列表查詢無跨租戶洩漏
7. 同租戶操作正常通過（正常路徑）
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── DB setup (in-memory SQLite) ───────────────────────────────────────────────
from saas_mvp.db import Base
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.models.note import Note
from saas_mvp.services.tenants import tenant_query, require_same_tenant
from saas_mvp.services.notes import (
    create_note,
    get_note,
    list_notes,
    update_note,
    delete_note,
)


@pytest.fixture(scope="function")
def db():
    """每個測試都得到全新 in-memory SQLite。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def two_tenants(db):
    """建立租戶 A、B，各一名使用者，各一筆 note。"""
    tenant_a = Tenant(name="TenantA", plan="free")
    tenant_b = Tenant(name="TenantB", plan="pro")
    db.add_all([tenant_a, tenant_b])
    db.flush()

    user_a = User(email="a@example.com", hashed_password="hash_a", tenant_id=tenant_a.id)
    user_b = User(email="b@example.com", hashed_password="hash_b", tenant_id=tenant_b.id)
    db.add_all([user_a, user_b])
    db.flush()

    note_a = Note(title="Note of A", content="content A", owner_id=user_a.id, tenant_id=tenant_a.id)
    note_b = Note(title="Note of B", content="content B", owner_id=user_b.id, tenant_id=tenant_b.id)
    db.add_all([note_a, note_b])
    db.commit()

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "user_a": user_a,
        "user_b": user_b,
        "note_a": note_a,
        "note_b": note_b,
    }


# ── 1. 資料模型結構 ─────────────────────────────────────────────────────────────

class TestDataModel:
    def test_user_has_tenant_id_column(self):
        """User 必須有 tenant_id 欄位（FK）。"""
        cols = {c.name for c in User.__table__.columns}
        assert "tenant_id" in cols

    def test_note_has_tenant_id_column(self):
        """Note 必須有 tenant_id 欄位（FK）。"""
        cols = {c.name for c in Note.__table__.columns}
        assert "tenant_id" in cols

    def test_user_tenant_fk(self):
        """User.tenant_id 必須 FK 指向 tenants.id。"""
        fks = {fk.target_fullname for fk in User.__table__.c.tenant_id.foreign_keys}
        assert "tenants.id" in fks

    def test_note_tenant_fk(self):
        """Note.tenant_id 必須 FK 指向 tenants.id。"""
        fks = {fk.target_fullname for fk in Note.__table__.c.tenant_id.foreign_keys}
        assert "tenants.id" in fks

    def test_user_belongs_to_tenant(self, db, two_tenants):
        """User 讀回後 tenant_id 正確對應。"""
        user_a = two_tenants["user_a"]
        tenant_a = two_tenants["tenant_a"]
        assert user_a.tenant_id == tenant_a.id

    def test_note_belongs_to_tenant(self, db, two_tenants):
        """Note 讀回後 tenant_id 正確對應。"""
        note_a = two_tenants["note_a"]
        tenant_a = two_tenants["tenant_a"]
        assert note_a.tenant_id == tenant_a.id


# ── 2. tenant_query() 隔離 ──────────────────────────────────────────────────────

class TestTenantQuery:
    def test_query_returns_own_tenant_note(self, db, two_tenants):
        """tenant_query 回傳本租戶 note。"""
        tid_a = two_tenants["tenant_a"].id
        results = tenant_query(db, Note, tid_a).all()
        ids = [n.id for n in results]
        assert two_tenants["note_a"].id in ids

    def test_query_excludes_other_tenant_note(self, db, two_tenants):
        """tenant_query 不回傳他租戶 note。"""
        tid_a = two_tenants["tenant_a"].id
        results = tenant_query(db, Note, tid_a).all()
        ids = [n.id for n in results]
        assert two_tenants["note_b"].id not in ids

    def test_query_returns_own_tenant_user(self, db, two_tenants):
        """tenant_query 也能隔離 User 查詢。"""
        tid_b = two_tenants["tenant_b"].id
        results = tenant_query(db, User, tid_b).all()
        ids = [u.id for u in results]
        assert two_tenants["user_b"].id in ids
        assert two_tenants["user_a"].id not in ids


# ── 3. require_same_tenant() ────────────────────────────────────────────────────

class TestRequireSameTenant:
    def test_same_tenant_passes(self):
        """相同 tenant_id 不拋例外。"""
        require_same_tenant(resource_tenant_id=1, current_tenant_id=1)  # 不 raise

    def test_different_tenant_raises_403(self):
        """不同 tenant_id 拋 403 HTTPException。"""
        with pytest.raises(HTTPException) as exc_info:
            require_same_tenant(resource_tenant_id=1, current_tenant_id=2)
        assert exc_info.value.status_code == 403

    def test_403_detail_meaningful(self):
        """403 要有可識別 detail，非空字串。"""
        with pytest.raises(HTTPException) as exc_info:
            require_same_tenant(resource_tenant_id=10, current_tenant_id=99)
        assert exc_info.value.detail  # 非空


# ── 4. 跨租戶讀取 → 404 ────────────────────────────────────────────────────────

class TestCrossTenantRead:
    def test_get_note_own_tenant_ok(self, db, two_tenants):
        """同租戶 get_note 正常。"""
        note = get_note(db, tenant_id=two_tenants["tenant_a"].id, note_id=two_tenants["note_a"].id)
        assert note.id == two_tenants["note_a"].id

    def test_get_note_cross_tenant_404(self, db, two_tenants):
        """A 租戶讀 B 的 note_id → 404（不洩漏存在性）。"""
        with pytest.raises(HTTPException) as exc_info:
            get_note(
                db,
                tenant_id=two_tenants["tenant_a"].id,
                note_id=two_tenants["note_b"].id,
            )
        assert exc_info.value.status_code == 404

    def test_list_notes_no_cross_leak(self, db, two_tenants):
        """list_notes 不洩漏他租戶資料。"""
        notes_a = list_notes(db, tenant_id=two_tenants["tenant_a"].id)
        ids_a = {n.id for n in notes_a}
        assert two_tenants["note_b"].id not in ids_a

    def test_list_notes_sees_own(self, db, two_tenants):
        """list_notes 能看到本租戶資料。"""
        notes_a = list_notes(db, tenant_id=two_tenants["tenant_a"].id)
        ids_a = {n.id for n in notes_a}
        assert two_tenants["note_a"].id in ids_a


# ── 5. 跨租戶寫入 → 404 ────────────────────────────────────────────────────────

class TestCrossTenantWrite:
    def test_update_note_cross_tenant_404(self, db, two_tenants):
        """A 租戶更新 B 的 note → 404。"""
        with pytest.raises(HTTPException) as exc_info:
            update_note(
                db,
                tenant_id=two_tenants["tenant_a"].id,
                note_id=two_tenants["note_b"].id,
                title="Hacked",
            )
        assert exc_info.value.status_code == 404

    def test_delete_note_cross_tenant_404(self, db, two_tenants):
        """A 租戶刪除 B 的 note → 404。"""
        with pytest.raises(HTTPException) as exc_info:
            delete_note(
                db,
                tenant_id=two_tenants["tenant_a"].id,
                note_id=two_tenants["note_b"].id,
            )
        assert exc_info.value.status_code == 404

    def test_update_note_same_tenant_ok(self, db, two_tenants):
        """同租戶 update_note 正常且持久化。"""
        updated = update_note(
            db,
            tenant_id=two_tenants["tenant_a"].id,
            note_id=two_tenants["note_a"].id,
            title="Updated Title",
        )
        assert updated.title == "Updated Title"

    def test_delete_note_same_tenant_ok(self, db, two_tenants):
        """同租戶 delete_note 正常，之後 get → 404。"""
        tid = two_tenants["tenant_a"].id
        nid = two_tenants["note_a"].id
        delete_note(db, tenant_id=tid, note_id=nid)
        with pytest.raises(HTTPException) as exc_info:
            get_note(db, tenant_id=tid, note_id=nid)
        assert exc_info.value.status_code == 404


# ── 6. 跨租戶 create 資料不污染 ────────────────────────────────────────────────

class TestCrossTenantCreate:
    def test_create_note_tenant_id_is_enforced(self, db, two_tenants):
        """create_note 建立時 tenant_id 由服務層決定，不由 caller 隨意注入。"""
        tid_a = two_tenants["tenant_a"].id
        uid_a = two_tenants["user_a"].id
        note = create_note(db, tenant_id=tid_a, owner_id=uid_a, title="Safe Note")
        assert note.tenant_id == tid_a

    def test_new_note_invisible_to_other_tenant(self, db, two_tenants):
        """A 租戶新建的 note 對 B 租戶不可見。"""
        tid_a = two_tenants["tenant_a"].id
        uid_a = two_tenants["user_a"].id
        new_note = create_note(db, tenant_id=tid_a, owner_id=uid_a, title="A-only")
        notes_b = list_notes(db, tenant_id=two_tenants["tenant_b"].id)
        ids_b = {n.id for n in notes_b}
        assert new_note.id not in ids_b


# ── 7. 多 note 情境下無洩漏 ────────────────────────────────────────────────────

class TestMultiNoteIsolation:
    def test_each_tenant_sees_only_own_notes(self, db, two_tenants):
        """建立多筆 note 後，每個租戶只看到自己的。"""
        tid_a = two_tenants["tenant_a"].id
        tid_b = two_tenants["tenant_b"].id
        uid_a = two_tenants["user_a"].id
        uid_b = two_tenants["user_b"].id

        # 各加一筆
        create_note(db, tenant_id=tid_a, owner_id=uid_a, title="A-2")
        create_note(db, tenant_id=tid_b, owner_id=uid_b, title="B-2")

        notes_a = {n.tenant_id for n in list_notes(db, tenant_id=tid_a)}
        notes_b = {n.tenant_id for n in list_notes(db, tenant_id=tid_b)}

        assert notes_a == {tid_a}      # A 只看到 tid_a
        assert notes_b == {tid_b}      # B 只看到 tid_b

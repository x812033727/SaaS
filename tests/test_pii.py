"""隱私保護模式 PII 表單測試（service + router + 租戶隔離）。"""

from __future__ import annotations

import datetime
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t  # noqa: F401,E402
from saas_mvp.models import customer as _c  # noqa: F401,E402
from saas_mvp.models import pii_request as _pii  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.pii_request import PII_SUBMITTED, PiiRequest  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import pii as pii_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def http(monkeypatch):
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
    app = create_app()

    def override_get_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _make_tenant(db, name="shop") -> int:
    t = Tenant(name=name, plan="free")
    db.add(t)
    db.commit()
    return t.id


class TestService:
    def test_create_request_issues_token(self, db, monkeypatch):
        monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
        tid = _make_tenant(db)
        req = pii_svc.create_request(db, tenant_id=tid, line_user_id="U1")
        assert req.token and len(req.token) > 20
        assert req.status == "pending"
        assert req.expires_at is not None
        assert pii_svc.form_url(req) == f"https://shop.example/pii/{req.token}"

    def test_submit_links_phone_and_birthday(self, db):
        tid = _make_tenant(db)
        req = pii_svc.create_request(db, tenant_id=tid, line_user_id="U1")
        c = pii_svc.submit(
            db, token=req.token, name="小明", phone="0912345678", birthday="1990-05-15"
        )
        assert c.tenant_id == tid and c.line_user_id == "U1"
        assert c.phone == "0912345678"
        assert c.birthday == datetime.date(1990, 5, 15)
        assert c.display_name == "小明"
        # 請求標記 submitted
        db.refresh(req)
        assert req.status == PII_SUBMITTED and req.submitted_at is not None

    def test_submit_birthday_slash_format(self, db):
        tid = _make_tenant(db)
        req = pii_svc.create_request(db, tenant_id=tid, line_user_id="U2")
        c = pii_svc.submit(db, token=req.token, name=None, phone=None, birthday="1985/12/01")
        assert c.birthday == datetime.date(1985, 12, 1)

    def test_submit_bad_birthday_ignored(self, db):
        tid = _make_tenant(db)
        req = pii_svc.create_request(db, tenant_id=tid, line_user_id="U3")
        c = pii_svc.submit(db, token=req.token, name=None, phone="0900", birthday="not-a-date")
        assert c.birthday is None and c.phone == "0900"

    def test_unknown_token_rejected(self, db):
        with pytest.raises(pii_svc.PiiTokenNotFound):
            pii_svc.submit(db, token="nope", name="x", phone="1", birthday=None)

    def test_expired_token_rejected(self, db):
        tid = _make_tenant(db)
        req = pii_svc.create_request(db, tenant_id=tid, line_user_id="U4", ttl_minutes=1)
        # 強制過期
        req.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        db.commit()
        with pytest.raises(pii_svc.PiiTokenExpired):
            pii_svc.submit(db, token=req.token, name="x", phone="1", birthday=None)

    def test_used_token_rejected_on_resubmit(self, db):
        tid = _make_tenant(db)
        req = pii_svc.create_request(db, tenant_id=tid, line_user_id="U5")
        pii_svc.submit(db, token=req.token, name="a", phone="111", birthday=None)
        with pytest.raises(pii_svc.PiiTokenAlreadyUsed):
            pii_svc.submit(db, token=req.token, name="b", phone="222", birthday=None)
        # 第二次未覆寫
        c = db.query(Customer).filter(Customer.line_user_id == "U5").first()
        assert c.phone == "111"

    def test_tenant_isolation_no_cross_write(self, db):
        a = _make_tenant(db, "A")
        b = _make_tenant(db, "B")
        # 兩租戶各自有 U-shared 顧客的請求
        req_a = pii_svc.create_request(db, tenant_id=a, line_user_id="Ushared")
        req_b = pii_svc.create_request(db, tenant_id=b, line_user_id="Ushared")
        pii_svc.submit(db, token=req_a.token, name="A方", phone="0911", birthday=None)
        pii_svc.submit(db, token=req_b.token, name="B方", phone="0922", birthday=None)
        ca = db.query(Customer).filter(
            Customer.tenant_id == a, Customer.line_user_id == "Ushared"
        ).first()
        cb = db.query(Customer).filter(
            Customer.tenant_id == b, Customer.line_user_id == "Ushared"
        ).first()
        # A 的 token 只寫到 A 租戶；B 不被污染
        assert ca.phone == "0911" and cb.phone == "0922"
        assert ca.id != cb.id


class TestRouter:
    def test_get_form_200(self, http):
        s = _Session()
        try:
            tid = _make_tenant(s)
            req = pii_svc.create_request(s, tenant_id=tid, line_user_id="U1")
            token = req.token
        finally:
            s.close()
        r = http.get(f"/pii/{token}")
        assert r.status_code == 200
        assert "填寫聯絡資訊" in r.text and token in r.text

    def test_get_unknown_token_404(self, http):
        r = http.get("/pii/doesnotexist")
        assert r.status_code == 404

    def test_post_submit_done_page_and_persists(self, http):
        s = _Session()
        try:
            tid = _make_tenant(s)
            req = pii_svc.create_request(s, tenant_id=tid, line_user_id="U9")
            token = req.token
        finally:
            s.close()
        r = http.post(
            f"/pii/{token}",
            data={"name": "阿華", "phone": "0987654321", "birthday": "2000-01-02"},
        )
        assert r.status_code == 200 and "已收到" in r.text
        s = _Session()
        try:
            c = s.query(Customer).filter(Customer.line_user_id == "U9").first()
            assert c.phone == "0987654321"
            assert c.birthday == datetime.date(2000, 1, 2)
        finally:
            s.close()

    def test_post_used_token_shows_used_state(self, http):
        s = _Session()
        try:
            tid = _make_tenant(s)
            req = pii_svc.create_request(s, tenant_id=tid, line_user_id="U10")
            token = req.token
        finally:
            s.close()
        http.post(f"/pii/{token}", data={"name": "x", "phone": "1", "birthday": ""})
        r2 = http.post(f"/pii/{token}", data={"name": "y", "phone": "2", "birthday": ""})
        assert r2.status_code == 200 and "已使用" in r2.text

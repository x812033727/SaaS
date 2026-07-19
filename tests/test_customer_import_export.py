"""顧客 CSV 匯入匯出 + line_user_id nullable。

驗收標準
--------
- 服務層:電話正規化、BOM 容忍、生日解析、壞列報告(列號+原因)
- all-or-nothing:任一列錯誤 → DB 零寫入
- 重複策略:同租戶+正規化電話,預設 skip;update_existing 覆寫非空欄位
- 檔內重複電話只建一筆;無電話列不去重
- UI:multipart 上傳成功、CSRF 開啟時 multipart 也要帶 token、匯出 round-trip
- 匯出:customers/products/services 三支 CSV,tenant-scoped
- 無 LINE 顧客(line_user_id=None)可存在且行銷派送不炸
"""

from __future__ import annotations

import csv
import io
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r, reservation_reminder as _rr  # noqa: F401,E402
from saas_mvp.models import campaign as _camp, campaign_send as _cs  # noqa: F401,E402
from saas_mvp.models import product as _p  # noqa: F401,E402
from saas_mvp.models import service as _s, service_category as _sc  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services.customer_import import (  # noqa: E402
    import_customers,
    normalize_phone,
)

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


def _tenant(db) -> int:
    t = Tenant(name=f"imp_{uuid.uuid4().hex[:6]}", plan="free")
    db.add(t)
    db.commit()
    return t.id


def _csv_bytes(rows: list[dict], *, header=None, bom=False) -> bytes:
    header = header or ["display_name", "phone", "birthday", "note"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    data = buf.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


class TestNormalizePhone:
    @pytest.mark.parametrize("raw,expected", [
        ("0912-345-678", "0912345678"),
        ("+886912345678", "0912345678"),
        ("09 1234 5678", "0912345678"),
        ("", None),
        (None, None),
    ])
    def test_cases(self, raw, expected):
        assert normalize_phone(raw) == expected


class TestImportService:
    def test_basic_import_with_bom(self, db):
        tid = _tenant(db)
        content = _csv_bytes([
            {"display_name": "王小明", "phone": "0912-345-678",
             "birthday": "1990-06-15", "note": "老客戶"},
            {"display_name": "李大華", "phone": "", "birthday": "", "note": ""},
        ], bom=True)
        report = import_customers(db, tenant_id=tid, content=content)
        assert report.ok and report.created == 2
        rows = list(db.execute(select(Customer)).scalars())
        assert len(rows) == 2
        wang = next(r for r in rows if r.display_name == "王小明")
        assert wang.phone == "0912345678"
        assert wang.birthday is not None and wang.birthday.month == 6
        assert wang.line_user_id is None

    def test_all_or_nothing_on_bad_row(self, db):
        tid = _tenant(db)
        content = _csv_bytes([
            {"display_name": "好列", "phone": "0911", "birthday": "", "note": ""},
            {"display_name": "", "phone": "", "birthday": "", "note": ""},  # 壞列
            {"display_name": "壞生日", "phone": "", "birthday": "not-a-date", "note": ""},
        ])
        report = import_customers(db, tenant_id=tid, content=content)
        assert not report.ok
        assert any("第 3 列" in e for e in report.errors)
        assert any("第 4 列" in e for e in report.errors)
        assert db.query(Customer).count() == 0  # 整批未寫入

    def test_duplicate_phone_skip_by_default(self, db):
        tid = _tenant(db)
        db.add(Customer(
            tenant_id=tid, line_user_id="Uexist",
            display_name="既有", phone="0912345678",
        ))
        db.commit()
        content = _csv_bytes([
            {"display_name": "新名字", "phone": "+886912345678",
             "birthday": "", "note": ""},
        ])
        report = import_customers(db, tenant_id=tid, content=content)
        assert report.skipped == 1 and report.created == 0
        db.expire_all()
        assert db.query(Customer).count() == 1
        assert db.query(Customer).one().display_name == "既有"  # 未覆寫

    def test_duplicate_phone_update_mode(self, db):
        tid = _tenant(db)
        db.add(Customer(
            tenant_id=tid, line_user_id="Uexist",
            display_name="既有", phone="0912345678", note="舊備註",
        ))
        db.commit()
        content = _csv_bytes([
            {"display_name": "新名字", "phone": "0912-345-678",
             "birthday": "1991-01-02", "note": ""},
        ])
        report = import_customers(
            db, tenant_id=tid, content=content, update_existing=True
        )
        assert report.updated == 1
        db.expire_all()
        c = db.query(Customer).one()
        assert c.display_name == "新名字"
        assert c.birthday is not None
        assert c.note == "舊備註"  # 空欄不覆寫
        assert c.line_user_id == "Uexist"  # LINE 綁定不動

    def test_in_file_duplicate_phone_creates_once(self, db):
        tid = _tenant(db)
        content = _csv_bytes([
            {"display_name": "A", "phone": "0911111111", "birthday": "", "note": ""},
            {"display_name": "B", "phone": "0911-111-111", "birthday": "", "note": ""},
        ])
        report = import_customers(db, tenant_id=tid, content=content)
        assert report.created == 1 and report.skipped == 1

    def test_missing_required_column(self, db):
        tid = _tenant(db)
        content = _csv_bytes(
            [{"phone": "0911"}], header=["phone"]
        )
        report = import_customers(db, tenant_id=tid, content=content)
        assert not report.ok
        assert any("display_name" in e for e in report.errors)

    def test_not_utf8_rejected(self, db):
        tid = _tenant(db)
        report = import_customers(
            db, tenant_id=tid, content="display_name\n王".encode("big5")
        )
        assert not report.ok

    def test_tenant_isolation_on_dedupe(self, db):
        """他租戶同電話不算重複。"""
        tid_a = _tenant(db)
        tid_b = _tenant(db)
        db.add(Customer(
            tenant_id=tid_a, line_user_id="Ua",
            display_name="A店顧客", phone="0912345678",
        ))
        db.commit()
        content = _csv_bytes([
            {"display_name": "B店新客", "phone": "0912345678",
             "birthday": "", "note": ""},
        ])
        report = import_customers(db, tenant_id=tid_b, content=content)
        assert report.created == 1


class TestNoLineCustomerSafety:
    def test_marketing_skips_no_line_customer(self, db):
        """無 LINE 顧客走行銷群發:標 failed(no_line_user_id),不炸。"""
        from saas_mvp.line_client import FakeLinePushClient
        from saas_mvp.models.campaign import CAMPAIGN_BROADCAST, Campaign
        from saas_mvp.services import features as features_svc
        from saas_mvp.services import marketing as marketing_svc
        import datetime as _dt

        tid = _tenant(db)
        features_svc.set_enabled(
            db, tid, features_svc.MARKETING_AUTO, True,
            actor_user_id=None, source="admin",
        )
        db.add(Customer(tenant_id=tid, line_user_id=None, display_name="無LINE"))
        camp = Campaign(
            tenant_id=tid, type=CAMPAIGN_BROADCAST, name="bc",
            message_template="hi {name}",
        )
        db.add(camp)
        db.commit()
        fake = FakeLinePushClient()
        r = marketing_svc.run_campaign(
            db, campaign=camp,
            now=_dt.datetime(2030, 6, 15, tzinfo=_dt.timezone.utc),
            cap=100, push_client=fake,
        )
        assert r["sent"] == 0 and r["skipped"] == 1
        assert fake.call_count == 0


# ── UI 端(multipart 上傳 / 匯出) ────────────────────────────────────────────


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _login(client) -> tuple[int, dict[str, str]]:
    """回傳 (tenant_id, Bearer headers)。R12-C3a:/ui 匯入/匯出頁已刪,
    endpoint 測試改打 API 層(header auth)。"""
    email = f"csv_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "password": "Test1234!",
        "tenant_name": f"csv_t_{uuid.uuid4().hex[:8]}",
    })
    assert r.status_code == 201
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    tid = client.get("/tenants/me", headers=headers).json()["id"]
    client.post("/ui/login", data={"email": email, "password": "Test1234!"},
                follow_redirects=False)
    return tid, headers


class TestExports:
    def test_customers_roundtrip(self, client):
        tid, headers = _login(client)
        db = _Session()
        try:
            db.add(Customer(
                tenant_id=tid, line_user_id=None,
                display_name="匯出客", phone="0911222333",
            ))
            db.commit()
        finally:
            db.close()
        r = client.get("/booking/customers/export.csv", headers=headers)
        assert r.status_code == 200
        assert "匯出客" in r.text
        # round-trip:匯出檔直接再匯入(v1 console)→ 同電話全 skip,不重複
        r2 = client.post(
            "/api/v1/customers/import",
            json={"content": r.text},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["ok"] is True
        db = _Session()
        try:
            assert db.query(Customer).filter(
                Customer.tenant_id == tid
            ).count() == 1
        finally:
            db.close()

    def test_products_and_services_export(self, client):
        tid, _headers = _login(client)
        db = _Session()
        try:
            from saas_mvp.services import catalog as catalog_svc
            from saas_mvp.services import shop as shop_svc

            shop_svc.create_product(
                db, tenant_id=tid, name="洗髮精", price_cents=39900
            )
            catalog_svc.create_service(
                db, tenant_id=tid, name="剪髮", duration_minutes=45,
                price_cents=80000,
            )
        finally:
            db.close()
        rp = client.get("/ui/products/export.csv")
        assert rp.status_code == 200 and "洗髮精" in rp.text
        rs = client.get("/ui/services/export.csv")
        assert rs.status_code == 200 and "剪髮" in rs.text

    def test_export_tenant_scoped(self, client):
        tid_a, _headers_a = _login(client)
        db = _Session()
        try:
            db.add(Customer(
                tenant_id=tid_a, line_user_id=None, display_name="A店專屬",
            ))
            db.commit()
        finally:
            db.close()
        client.get("/ui/logout")
        _tid_b, headers_b = _login(client)
        r = client.get("/booking/customers/export.csv", headers=headers_b)
        assert "A店專屬" not in r.text

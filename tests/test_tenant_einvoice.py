"""R5-C2 — 店家自有電子發票(opt-in):設定/issuer 工廠/訂單與定金開立。

覆蓋:
- save_config:驗證(env/啟用需憑證齊)、加密存放、遮罩重存沿用舊 key
- issuer_for_tenant:未啟用/不齊 None;齊備回 per-tenant Ecpay issuer
- issue_for_order:未啟用零落列(現狀不變);啟用(stub 注入)開立成功、
  買受人=顧客 email/名、品名=商品/服務消費、冪等
- issue_for_deposit:同上,品名=預約定金;deposit_cents<=0 不開
- mark_order_paid / deposit.mark_paid 掛點:未啟用不產生任何發票列
- _attempt_issue:入列後店家停用 → failed 留訊息
- /ui/billing/einvoice-config 表單:owner 儲存 303、非法 env 400
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault(
    "SAAS_LINE_CHANNEL_ENCRYPT_KEY",
    "ZGV2LWxpbmUtc2VjcmV0LWtleS0zMmJ5dGVzLWxvbmc=",
)

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r  # noqa: F401,E402
import saas_mvp.models.invoice as _inv  # noqa: F401,E402
import saas_mvp.models.order as _o  # noqa: F401,E402
import saas_mvp.models.tenant_einvoice_config as _tec  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.invoice import INVOICE_FAILED, INVOICE_ISSUED, Invoice  # noqa: E402
from saas_mvp.models.order import Order  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import deposit as deposit_svc  # noqa: E402
from saas_mvp.services import invoices as invoices_svc  # noqa: E402
from saas_mvp.services import shop as shop_svc  # noqa: E402
from saas_mvp.services import tenant_einvoice as einvoice_svc  # noqa: E402
from saas_mvp.services.invoice_ecpay import (  # noqa: E402
    EcpayInvoiceIssuer,
    StubInvoiceIssuer,
)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


def _seed(*, with_email=True):
    db = _Session()
    try:
        t = Tenant(name=f"ei_{uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        cust = Customer(
            tenant_id=t.id,
            display_name="發票客",
            email="buyer@mail.tw" if with_email else None,
        )
        db.add(cust)
        db.commit()
        return t.id, cust.id
    finally:
        db.close()


def _enable(tid, **overrides):
    db = _Session()
    try:
        einvoice_svc.save_config(
            db,
            tenant_id=tid,
            merchant_id=overrides.get("merchant_id", "2000132"),
            hash_key=overrides.get("hash_key", "ejCk326UnaZWKisg"),
            hash_iv=overrides.get("hash_iv", "q9jcZX8Ib9LM8wYk"),
            environment=overrides.get("environment", "stage"),
            enabled=overrides.get("enabled", True),
        )
    finally:
        db.close()


def _make_order(tid, cid, cents=50000) -> int:
    db = _Session()
    try:
        order = Order(
            tenant_id=tid, customer_id=cid, total_cents=cents, status="pending"
        )
        db.add(order)
        db.commit()
        return order.id
    finally:
        db.close()


class TestConfig:
    def test_save_encrypts_and_masked_resave_keeps_keys(self):
        tid, _ = _seed()
        _enable(tid)
        db = _Session()
        try:
            cfg = einvoice_svc.get_config(db, tid)
            assert cfg.enabled and cfg.is_complete
            assert b"ejCk326UnaZWKisg" not in (cfg.hash_key_enc or b"")  # 加密
            assert cfg.hash_key == "ejCk326UnaZWKisg"  # 可解回
            # 遮罩重存:key/iv 留空 → 沿用舊值
            einvoice_svc.save_config(
                db, tenant_id=tid, merchant_id="2000132",
                hash_key="", hash_iv="", enabled=True,
            )
            cfg2 = einvoice_svc.get_config(db, tid)
            assert cfg2.hash_key == "ejCk326UnaZWKisg"
        finally:
            db.close()

    def test_enable_requires_complete_credentials(self):
        tid, _ = _seed()
        db = _Session()
        try:
            with pytest.raises(einvoice_svc.EinvoiceConfigError):
                einvoice_svc.save_config(
                    db, tenant_id=tid, merchant_id="2000132", enabled=True
                )
            with pytest.raises(einvoice_svc.EinvoiceConfigError):
                einvoice_svc.save_config(
                    db, tenant_id=tid, merchant_id="x",
                    hash_key="k", hash_iv="v", environment="bogus",
                )
        finally:
            db.close()

    def test_issuer_factory(self):
        tid, _ = _seed()
        db = _Session()
        try:
            assert einvoice_svc.issuer_for_tenant(db, tid) is None  # 無設定
        finally:
            db.close()
        _enable(tid, enabled=False)
        db = _Session()
        try:
            assert einvoice_svc.issuer_for_tenant(db, tid) is None  # 未啟用
        finally:
            db.close()
        _enable(tid)
        db = _Session()
        try:
            issuer = einvoice_svc.issuer_for_tenant(db, tid)
            assert isinstance(issuer, EcpayInvoiceIssuer)
        finally:
            db.close()


class TestIssueForOrder:
    def test_disabled_no_invoice_row(self):
        tid, cid = _seed()
        oid = _make_order(tid, cid)
        db = _Session()
        try:
            order = db.get(Order, oid)
            assert invoices_svc.issue_for_order(db, order) is None
            assert db.query(Invoice).count() == 0
        finally:
            db.close()

    def test_enabled_issues_with_customer_buyer_and_idempotent(self):
        tid, cid = _seed()
        _enable(tid)
        oid = _make_order(tid, cid, cents=123400)
        stub = StubInvoiceIssuer()
        db = _Session()
        try:
            order = db.get(Order, oid)
            row = invoices_svc.issue_for_order(db, order, issuer=stub)
            assert row.status == INVOICE_ISSUED
            assert row.order_id == oid and row.reservation_id is None
            assert stub.issued[0]["item_name"] == "商品/服務消費"
            assert stub.issued[0]["buyer_email"] == "buyer@mail.tw"
            assert stub.issued[0]["buyer_name"] == "發票客"
            assert stub.issued[0]["amount_twd"] == 1234
            again = invoices_svc.issue_for_order(db, order, issuer=stub)
            assert again.id == row.id and len(stub.issued) == 1  # 冪等
        finally:
            db.close()

    def test_mark_order_paid_hook_disabled_noop(self):
        tid, cid = _seed()
        oid = _make_order(tid, cid)
        db = _Session()
        try:
            shop_svc.mark_order_paid(db, tenant_id=tid, order_id=oid)
            assert db.query(Invoice).count() == 0  # 未啟用=現狀不變
        finally:
            db.close()

    def test_mark_order_paid_hook_enabled_issues(self, monkeypatch):
        tid, cid = _seed()
        _enable(tid)
        stub = StubInvoiceIssuer()
        monkeypatch.setattr(
            einvoice_svc, "issuer_for_tenant", lambda db, t: stub
        )
        oid = _make_order(tid, cid)
        db = _Session()
        try:
            shop_svc.mark_order_paid(db, tenant_id=tid, order_id=oid)
            row = db.query(Invoice).one()
            assert row.order_id == oid and row.status == INVOICE_ISSUED
            # 回調重放:再標一次不重開
            shop_svc.mark_order_paid(db, tenant_id=tid, order_id=oid)
            assert db.query(Invoice).count() == 1
        finally:
            db.close()


class TestIssueForDeposit:
    def _resv_with_deposit(self, tid, cid, cents=20000) -> int:
        db = _Session()
        try:
            slot = BookingSlot(
                tenant_id=tid, slot_start=_SLOT_START, max_capacity=4
            )
            db.add(slot)
            db.flush()
            resv = Reservation(
                tenant_id=tid, slot_id=slot.id, party_size=1,
                status="confirmed", customer_id=cid,
                deposit_status="pending", deposit_cents=cents,
            )
            db.add(resv)
            db.commit()
            return resv.id
        finally:
            db.close()

    def test_deposit_mark_paid_enabled_issues(self, monkeypatch):
        tid, cid = _seed()
        _enable(tid)
        stub = StubInvoiceIssuer()
        monkeypatch.setattr(
            einvoice_svc, "issuer_for_tenant", lambda db, t: stub
        )
        rid = self._resv_with_deposit(tid, cid)
        db = _Session()
        try:
            resv = db.get(Reservation, rid)
            assert deposit_svc.mark_paid(db, resv) is True
            row = db.query(Invoice).one()
            assert row.reservation_id == rid and row.order_id is None
            assert stub.issued[0]["item_name"] == "預約定金"
            assert stub.issued[0]["amount_twd"] == 200
            # 重送回調:冪等
            assert deposit_svc.mark_paid(db, resv) is True
            assert db.query(Invoice).count() == 1
        finally:
            db.close()

    def test_deposit_disabled_noop(self):
        tid, cid = _seed()
        rid = self._resv_with_deposit(tid, cid)
        db = _Session()
        try:
            resv = db.get(Reservation, rid)
            assert deposit_svc.mark_paid(db, resv) is True
            assert db.query(Invoice).count() == 0
        finally:
            db.close()

    def test_zero_deposit_no_invoice(self, monkeypatch):
        tid, cid = _seed()
        _enable(tid)
        monkeypatch.setattr(
            einvoice_svc, "issuer_for_tenant",
            lambda db, t: StubInvoiceIssuer(),
        )
        rid = self._resv_with_deposit(tid, cid, cents=0)
        db = _Session()
        try:
            resv = db.get(Reservation, rid)
            assert invoices_svc.issue_for_deposit(db, resv) is None
            assert db.query(Invoice).count() == 0
        finally:
            db.close()


class TestAttemptIssueDisabledAfterQueue:
    def test_marks_failed_with_message(self):
        tid, cid = _seed()
        oid = _make_order(tid, cid)
        db = _Session()
        try:
            row = Invoice(
                tenant_id=tid, order_id=oid, relate_number="ODX",
                amount_cents=100, status="pending", provider="ecpay",
            )
            db.add(row)
            db.commit()
            invoices_svc._attempt_issue(db, row)  # 店家未啟用
            assert row.status == INVOICE_FAILED
            assert "未啟用" in row.error_msg
        finally:
            db.close()


class TestBillingUi:
    @pytest.fixture()
    def client(self):
        app = create_app()

        def override_db():
            s = _Session()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = override_db
        with TestClient(app, follow_redirects=False) as c:
            yield c

    def _login(self, client) -> None:
        email = f"ei_{uuid.uuid4().hex[:8]}@x.tw"
        r = client.post("/auth/register", json={
            "email": email, "password": "Test1234!",
            "tenant_name": f"EI {uuid.uuid4().hex[:6]}",
        })
        assert r.status_code == 201
        assert client.post(
            "/ui/login", data={"email": email, "password": "Test1234!"}
        ).status_code == 303

    def test_save_and_invalid_env(self, client):
        self._login(client)
        ok = client.post("/ui/billing/einvoice-config", data={
            "merchant_id": "2000132", "hash_key": "k1", "hash_iv": "v1",
            "environment": "stage", "enabled": "true",
        })
        assert ok.status_code == 303
        page = client.get("/ui/billing")
        assert "店家電子發票" in page.text
        bad = client.post("/ui/billing/einvoice-config", data={
            "merchant_id": "2000132", "environment": "bogus",
        })
        assert bad.status_code == 400

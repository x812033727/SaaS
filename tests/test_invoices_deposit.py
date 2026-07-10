"""R2-3 測試 — C2 電子發票 + C4 定金。"""

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

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.line_client import FakeLinePushClient  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.invoice import Invoice  # noqa: E402
from saas_mvp.models.reservation import RESERVATION_CANCELLED, Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.user import User  # noqa: E402
from saas_mvp.ops.cancel_unpaid_deposits import cancel_unpaid_deposits  # noqa: E402
from saas_mvp.ops.retry_failed_invoices import retry_failed_invoices  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import deposit as deposit_svc  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import invoices as invoices_svc  # noqa: E402
from saas_mvp.services import subscriptions as subs_svc  # noqa: E402
from saas_mvp.services.invoice_ecpay import (  # noqa: E402
    EcpayInvoiceIssuer,
    InvoiceError,
    StubInvoiceIssuer,
    aes_decrypt_data,
    aes_encrypt_data,
)

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 15, 9, 0, tzinfo=datetime.timezone.utc)
_KEY = "a" * 16
_IV = "b" * 16


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, **kw) -> Tenant:
    t = Tenant(name=f"iv_{uuid.uuid4().hex[:8]}", plan="pro", **kw)
    db.add(t)
    db.flush()
    db.add(User(email=f"{t.name}@x.tw", hashed_password="x",
                tenant_id=t.id, role="owner"))
    db.commit()
    db.refresh(t)
    return t


def _charge(db, tenant) -> object:
    sub = subs_svc.create_subscription(
        db, tenant_id=tenant.id, feature=features_svc.BUNDLE_PRO, amount_cents=89900
    )
    subs_svc.activate(db, sub)
    from saas_mvp.models.subscription_charge import SubscriptionCharge

    return db.execute(
        select(SubscriptionCharge).where(
            SubscriptionCharge.subscription_id == sub.id
        )
    ).scalar_one()


# ── C2 發票 ──────────────────────────────────────────────────────────────────

class TestInvoiceAes:
    def test_roundtrip(self):
        payload = {"RelateNumber": "SC1T99", "SalesAmount": 899, "中文": "測試"}
        enc = aes_encrypt_data(payload, _KEY, _IV)
        assert aes_decrypt_data(enc, _KEY, _IV) == payload


class TestIssueForCharge:
    def test_stub_issue_and_idempotent(self, db):
        t = _tenant(db)
        charge = _charge(db, t)
        issuer = StubInvoiceIssuer()
        inv = invoices_svc.issue_for_charge(db, charge, issuer=issuer)
        assert inv.status == "issued"
        assert inv.invoice_no.startswith("ST")
        assert inv.buyer_email.endswith("@x.tw")
        # 回調重放:不重開
        again = invoices_svc.issue_for_charge(db, charge, issuer=issuer)
        assert again.id == inv.id
        assert len(issuer.issued) == 1

    def test_issuer_failure_marks_failed_not_raises(self, db):
        class Boom(StubInvoiceIssuer):
            def issue(self, **kw):
                raise InvoiceError("api down")

        t = _tenant(db)
        charge = _charge(db, t)
        inv = invoices_svc.issue_for_charge(db, charge, issuer=Boom())
        assert inv.status == "failed" and "api down" in inv.error_msg

    def test_retry_failed_invoices(self, db):
        class Boom(StubInvoiceIssuer):
            def issue(self, **kw):
                raise InvoiceError("api down")

        t = _tenant(db)
        charge = _charge(db, t)
        invoices_svc.issue_for_charge(db, charge, issuer=Boom())
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        ok_issuer = StubInvoiceIssuer()
        results = retry_failed_invoices(
            session_factory=factory, issuer=ok_issuer, apply=True,
        )  # 用真實 now(發票 created_at 是真實時間,假 now 會誤判 too_old)
        assert [r.status for r in results] == ["issued"]

    def test_ecpay_issuer_payload_encrypts(self, monkeypatch):
        monkeypatch.setattr(settings, "ecpay_invoice_merchant_id", "2000132")
        monkeypatch.setattr(settings, "ecpay_invoice_hash_key", _KEY)
        monkeypatch.setattr(settings, "ecpay_invoice_hash_iv", _IV)
        captured = {}

        def fake_post(url, body):
            import json

            captured["url"] = url
            envelope = json.loads(body)
            captured["data"] = aes_decrypt_data(envelope["Data"], _KEY, _IV)
            resp_data = aes_encrypt_data(
                {"RtnCode": "1", "InvoiceNo": "AB12345678",
                 "InvoiceDate": "2030-06-15", "RandomNumber": "1234"},
                _KEY, _IV,
            )
            return json.dumps({"TransCode": "1", "Data": resp_data})

        issuer = EcpayInvoiceIssuer(http_post=fake_post)
        result = issuer.issue(
            relate_number="SC1T99", amount_twd=899,
            buyer_email="o@x.tw", item_name="月費",
        )
        assert result.invoice_no == "AB12345678"
        assert captured["data"]["SalesAmount"] == 899
        assert "einvoice-stage" in captured["url"]


# ── C4 定金 ──────────────────────────────────────────────────────────────────

def _deposit_tenant(db) -> tuple[Tenant, int]:
    t = _tenant(db, deposit_cents=20000, deposit_hold_minutes=30)
    slot = BookingSlot(
        tenant_id=t.id,
        slot_start=_NOW + datetime.timedelta(days=1),
        max_capacity=4,
    )
    db.add(slot)
    db.commit()
    return t, slot.id


class TestDeposit:
    def test_online_booking_snapshots_deposit(self, db):
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=2,
            line_user_id="Udep1",
        )
        assert resv.deposit_status == "pending"
        assert resv.deposit_cents == 20000
        assert resv.deposit_merchant_trade_no.startswith("DP")
        assert resv.deposit_expires_at is not None

    def test_manual_booking_no_deposit(self, db):
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1,
            line_user_id=None,  # 店家手動
        )
        assert resv.deposit_status is None

    def test_flag_off_no_deposit(self, db, monkeypatch):
        monkeypatch.setattr(settings, "features_default_enabled", False)
        t, slot_id = _deposit_tenant(db)
        t.plan = "standard"  # standard 無 DEPOSIT_PAYMENT
        db.commit()
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1,
            line_user_id="Udep2",
        )
        assert resv.deposit_status is None

    def test_mark_paid_idempotent_and_expired_rejected(self, db):
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1,
            line_user_id="Udep3",
        )
        assert deposit_svc.mark_paid(db, resv) is True
        assert deposit_svc.mark_paid(db, resv) is True  # 冪等
        resv.deposit_status = deposit_svc.DEPOSIT_EXPIRED
        db.commit()
        assert deposit_svc.mark_paid(db, resv) is False

    def test_expired_pending_cancelled_with_refill_and_notify(self, db):
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=2,
            line_user_id="Udep4",
        )
        # 模擬逾時
        resv.deposit_expires_at = _NOW - datetime.timedelta(minutes=1)
        db.commit()
        # LINE 設定(通知用)
        from saas_mvp.models.line_channel_config import LineChannelConfig

        cfg = LineChannelConfig(tenant_id=t.id, default_target_lang="zh-TW")
        cfg.channel_secret = "s" * 32
        cfg.access_token = "tok"
        db.add(cfg)
        db.commit()

        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        fake = FakeLinePushClient()
        results = cancel_unpaid_deposits(
            session_factory=factory, push_client=fake, apply=True, now=_NOW
        )
        assert [r.status for r in results] == ["cancelled"]
        db.expire_all()
        resv2 = db.get(Reservation, resv.id)
        slot = db.get(BookingSlot, slot_id)
        assert resv2.status == RESERVATION_CANCELLED
        assert resv2.deposit_status == "expired"
        assert slot.booked_count == 0  # 名額回補
        assert fake.call_count == 1 and "自動取消" in fake.sent[0].text

    def test_paid_not_cancelled(self, db):
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1,
            line_user_id="Udep5",
        )
        deposit_svc.mark_paid(db, resv)
        resv.deposit_expires_at = _NOW - datetime.timedelta(minutes=1)
        db.commit()
        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        assert cancel_unpaid_deposits(
            session_factory=factory, push_client=FakeLinePushClient(),
            apply=True, now=_NOW,
        ) == []


# ── 付款頁/回調端到端(stub 模擬頁)────────────────────────────────────────────

class TestDepositEndpoints:
    @pytest.fixture()
    def client(self):
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
        with TestClient(app) as c:
            yield c

    def test_stub_payment_page_and_simulated_pay(self, client):
        db = _Session()
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1,
            line_user_id="Uweb1",
        )
        rid = resv.id
        db.close()

        r = client.get(f"/payments/ecpay/deposit/{rid}")
        assert r.status_code == 200 and "模擬定金付款" in r.text
        r = client.post(f"/payments/stub/deposit-paid/{rid}")
        assert "已付款" in r.text
        db = _Session()
        try:
            assert db.get(Reservation, rid).deposit_status == "paid"
        finally:
            db.close()

    def test_paid_page_shows_confirmed(self, client):
        db = _Session()
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1,
            line_user_id="Uweb2",
        )
        deposit_svc.mark_paid(db, resv)
        rid = resv.id
        db.close()
        r = client.get(f"/payments/ecpay/deposit/{rid}")
        assert "已付款" in r.text

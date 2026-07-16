"""R2-5 測試 — E2 LINE Pay + E1 Google Calendar。"""

from __future__ import annotations

import datetime
import json
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
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.order import ORDER_PAID, ORDER_PENDING, Order  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import gcal as gcal_svc  # noqa: E402
from saas_mvp.services.gcal import StubGcalClient  # noqa: E402
from saas_mvp.services.payment import get_payment_provider  # noqa: E402
from saas_mvp.services.payment_linepay import (  # noqa: E402
    LinePayClient,
    LinePayError,
    sign,
)

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


# ── E2 LINE Pay ──────────────────────────────────────────────────────────────

class TestLinePaySign:
    def test_known_vector(self):
        # 固定輸入的簽章必須穩定(離線可驗;值由本實作首次計算後釘住防回歸)
        out = sign("secret", "/v3/payments/request", '{"amount":100}', "nonce-1")
        import base64
        import hashlib
        import hmac as _hmac

        expected = base64.b64encode(
            _hmac.new(
                b"secret",
                b'secret/v3/payments/request{"amount":100}nonce-1',
                hashlib.sha256,
            ).digest()
        ).decode()
        assert out == expected


class TestLinePayClient:
    def _client(self, responses, monkeypatch):
        monkeypatch.setattr(settings, "line_pay_channel_id", "chan")
        monkeypatch.setattr(settings, "line_pay_channel_secret", "sec")
        calls = []

        def fake_post(url, body, headers):
            calls.append({"url": url, "body": json.loads(body), "headers": headers})
            return json.dumps(responses.pop(0))

        c = LinePayClient(http_post=fake_post)
        return c, calls

    def test_request_payment(self, monkeypatch):
        c, calls = self._client([{
            "returnCode": "0000",
            "info": {"transactionId": 987654321,
                     "paymentUrl": {"web": "https://sandbox-web-pay.line.me/x"}},
        }], monkeypatch)
        out = c.request_payment(
            order_id=5, amount_twd=800, currency="TWD",
            confirm_url="https://x/c", cancel_url="https://x/k", item_name="訂單 5",
        )
        assert out["transaction_id"] == "987654321"
        assert "sandbox-api-pay.line.me" in calls[0]["url"]
        assert calls[0]["headers"]["X-LINE-ChannelId"] == "chan"
        assert calls[0]["body"]["amount"] == 800

    def test_confirm_idempotent_code(self, monkeypatch):
        c, _ = self._client([{"returnCode": "1169", "returnMessage": "already"}], monkeypatch)
        # 已 confirm 錯誤碼視為成功
        c.confirm_payment(transaction_id="1", amount_twd=800, currency="TWD")

    def test_confirm_rejected(self, monkeypatch):
        c, _ = self._client([{"returnCode": "1104", "returnMessage": "bad"}], monkeypatch)
        with pytest.raises(LinePayError):
            c.confirm_payment(transaction_id="1", amount_twd=800, currency="TWD")

    def test_provider_dispatch(self, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "linepay")
        assert get_payment_provider().name() == "linepay"


class TestLinePayEndpoints:
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

    def _order(self, status=ORDER_PENDING, total=80000, txn_id="42") -> tuple[int, str]:
        """建單並回 (order_id, trade_no)。confirm URL 以不可猜 trade_no 為鍵
        (PEA-3);payment_txn_id 模擬 create_checkout 時已落庫的 txid 綁定。"""
        db = _Session()
        try:
            t = Tenant(name=f"lp_{uuid.uuid4().hex[:6]}", plan="pro")
            db.add(t)
            db.flush()
            trade_no = f"ODLP{uuid.uuid4().hex[:14].upper()}"
            o = Order(tenant_id=t.id, line_user_id="U1",
                      status=status, total_cents=total,
                      merchant_trade_no=trade_no, payment_txn_id=txn_id)
            db.add(o)
            db.commit()
            return o.id, trade_no
        finally:
            db.close()

    def test_confirm_marks_paid(self, client, monkeypatch):
        import saas_mvp.services.payment_linepay as lp

        monkeypatch.setattr(settings, "line_pay_channel_id", "chan")
        monkeypatch.setattr(settings, "line_pay_channel_secret", "sec")
        monkeypatch.setattr(
            lp, "_urllib_post",
            lambda url, body, headers: json.dumps({"returnCode": "0000", "info": {}}),
        )
        oid, trade_no = self._order()
        r = client.get(f"/payments/linepay/confirm?transactionId=42&orderId={trade_no}")
        assert "付款完成" in r.text
        db = _Session()
        try:
            o = db.get(Order, oid)
            assert o.status == ORDER_PAID
            # 結帳鍵不再被 LP{txid} 覆寫;txid 綁定保留於 payment_txn_id
            assert o.merchant_trade_no == trade_no
            assert o.payment_txn_id == "42"
        finally:
            db.close()

    def test_confirm_already_paid_short_circuit(self, client):
        _, trade_no = self._order(status=ORDER_PAID)
        r = client.get(f"/payments/linepay/confirm?transactionId=42&orderId={trade_no}")
        assert "付款完成" in r.text  # 不打 API 直接成功頁

    def test_confirm_txid_mismatch_rejected(self, client):
        """txid↔order 綁定:transactionId 與落庫值不符 → 400 且不 confirm。"""
        oid, trade_no = self._order(txn_id="42")
        r = client.get(f"/payments/linepay/confirm?transactionId=999&orderId={trade_no}")
        assert r.status_code == 400
        db = _Session()
        try:
            assert db.get(Order, oid).status == ORDER_PENDING
        finally:
            db.close()

    def test_confirm_enumeration_404(self, client):
        """整數 order id / 未知 trade_no 一律 404(PEA-3 防枚舉)。"""
        oid, _ = self._order()
        assert client.get(
            f"/payments/linepay/confirm?transactionId=42&orderId={oid}"
        ).status_code == 404
        assert client.get(
            "/payments/linepay/confirm?transactionId=42&orderId=ODUNKNOWN"
        ).status_code == 404

    def test_confirm_api_reject_no_paid(self, client, monkeypatch):
        import saas_mvp.services.payment_linepay as lp

        monkeypatch.setattr(settings, "line_pay_channel_id", "chan")
        monkeypatch.setattr(settings, "line_pay_channel_secret", "sec")
        monkeypatch.setattr(
            lp, "_urllib_post",
            lambda url, body, headers: json.dumps({"returnCode": "1104"}),
        )
        oid, trade_no = self._order()
        r = client.get(f"/payments/linepay/confirm?transactionId=42&orderId={trade_no}")
        assert r.status_code == 502
        db = _Session()
        try:
            assert db.get(Order, oid).status == ORDER_PENDING
        finally:
            db.close()


# ── E1 GCal ──────────────────────────────────────────────────────────────────

def _gcal_tenant(db) -> tuple[Tenant, int]:
    t = Tenant(name=f"gc_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(t)
    db.flush()
    cred = TenantGcalCredential(tenant_id=t.id, calendar_id="primary")
    cred.refresh_token = "rt-secret"
    db.add(cred)
    slot = BookingSlot(
        tenant_id=t.id,
        slot_start=datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc),
        max_capacity=4,
    )
    db.add(slot)
    db.commit()
    return t, slot.id


class TestGcalSync:
    def test_lifecycle_create_reschedule_cancel(self, db, monkeypatch):
        t, slot_id = _gcal_tenant(db)
        stub = StubGcalClient()
        monkeypatch.setattr(gcal_svc, "get_gcal_client", lambda db=None: stub)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=2, line_user_id="Ugc"
        )
        assert resv.gcal_event_id in stub.events
        eid = resv.gcal_event_id

        gcal_svc.sync_reservation(db, resv, "reschedule", client=stub)
        assert eid in stub.events
        gcal_svc.sync_reservation(db, resv, "cancel", client=stub)
        assert eid not in stub.events

    def test_no_credential_noop(self, db):
        t = Tenant(name=f"nogc_{uuid.uuid4().hex[:6]}", plan="pro")
        db.add(t)
        db.flush()
        slot = BookingSlot(
            tenant_id=t.id,
            slot_start=datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc),
            max_capacity=4,
        )
        db.add(slot)
        db.commit()
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot.id, party_size=1, line_user_id="Un"
        )
        assert resv.gcal_event_id is None  # 未連結:不產生 event

    def test_client_error_never_breaks_booking(self, db, monkeypatch):
        class Boom(StubGcalClient):
            def insert_event(self, **kw):
                raise gcal_svc.GcalError("api down")

        monkeypatch.setattr(gcal_svc, "get_gcal_client", lambda db=None: Boom())
        t, slot_id = _gcal_tenant(db)
        booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1, line_user_id="Ub"
        )
        # 預約成功、憑證標 error、last_error 記錄
        cred = db.execute(select(TenantGcalCredential).where(
            TenantGcalCredential.tenant_id == t.id
        )).scalar_one()
        assert cred.status == "error" and "api down" in cred.last_error

    def test_missing_event_is_rebuilt_on_reschedule(self, db):
        """先前 create 失敗時，改期仍須補建目前最新版事件。"""
        t, slot_id = _gcal_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1, line_user_id="Unoop"
        )
        # 模擬先前同步失敗留下的錯誤狀態，且沒有 event_id。
        cred = db.execute(select(TenantGcalCredential).where(
            TenantGcalCredential.tenant_id == t.id)).scalar_one()
        cred.status = "error"
        cred.last_error = "prev fail"
        resv.gcal_event_id = None
        db.flush()

        stub = StubGcalClient()
        gcal_svc.sync_reservation(db, resv, "reschedule", client=stub)

        db.refresh(cred)
        assert cred.status == "connected"
        assert cred.last_error is None
        assert resv.gcal_event_id in stub.events

    def test_refresh_token_encrypted_at_rest(self, db):
        t, _ = _gcal_tenant(db)
        cred = db.execute(select(TenantGcalCredential).where(
            TenantGcalCredential.tenant_id == t.id
        )).scalar_one()
        assert b"rt-secret" not in bytes(cred.refresh_token_enc)
        assert cred.refresh_token == "rt-secret"

"""R6-A3 — 訂單閘道退款(三金流 + 手動對帳 + 崩潰安全)。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.db import Base  # noqa: E402
from saas_mvp.models.order import ORDER_PAID, Order  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import order_refund as refund_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_HK, _HIV = "k" * 32, "i" * 16


@pytest.fixture(autouse=True)
def _fresh():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield


def _paid_order(provider, *, total=80000, txn="LP1", ptrade="NP1", pmid="MID") -> tuple[int, int]:
    db = _Session()
    try:
        t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="pro")
        db.add(t)
        db.flush()
        o = Order(
            tenant_id=t.id, line_user_id="U1", status=ORDER_PAID, total_cents=total,
            merchant_trade_no=f"OD{uuid.uuid4().hex[:12].upper()}",
            payment_txn_id=txn, payment_provider=provider,
            provider_trade_no=ptrade, provider_merchant_id=pmid,
        )
        db.add(o)
        db.commit()
        return o.id, t.id
    finally:
        db.close()


def _get(oid) -> Order:
    db = _Session()
    try:
        return db.get(Order, oid)
    finally:
        db.close()


class _FakeLine:
    def __init__(self, code="0000", raises=None):
        self.code, self.raises, self.calls = code, raises, []

    def refund(self, *, transaction_id, refund_amount_twd):
        self.calls.append((transaction_id, refund_amount_twd))
        if self.raises:
            raise self.raises
        return {"returnCode": self.code, "returnMessage": "m"}


class _FakeNeweb:
    def __init__(self, status="SUCCESS", raises=None):
        self.status, self.raises, self.calls = status, raises, []

    def refund(self, *, merchant_order_no, trade_no, amount_twd):
        self.calls.append((merchant_order_no, trade_no, amount_twd))
        if self.raises:
            raise self.raises
        return {"Status": self.status, "Message": "m", "Result": {}}


class TestStubAndLinePay:
    def test_stub_full_refund(self):
        oid, tid = _paid_order("stub")
        db = _Session()
        try:
            o = refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1)
            assert o.refund_status == "refunded" and o.refunded_cents == 80000
        finally:
            db.close()

    def test_linepay_partial_then_partial(self):
        oid, tid = _paid_order("linepay")
        fake = _FakeLine()
        db = _Session()
        try:
            o = refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, amount_cents=30000, linepay_client=fake)
            assert o.refund_status == "partially_refunded"
            o = refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, amount_cents=50000, linepay_client=fake)
            assert o.refund_status == "refunded"
            assert len(fake.calls) == 2
        finally:
            db.close()

    def test_linepay_timeout_manual(self):
        oid, tid = _paid_order("linepay")
        db = _Session()
        try:
            with pytest.raises(refund_svc.OrderRefundError):
                refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, linepay_client=_FakeLine(raises=RuntimeError("t")))
        finally:
            db.close()
        assert _get(oid).refund_status == "manual_required"


class TestNewebPay:
    def test_success(self):
        oid, tid = _paid_order("newebpay")
        db = _Session()
        try:
            o = refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, newebpay_client=_FakeNeweb())
            assert o.refund_status == "refunded"
        finally:
            db.close()

    def test_rejected_failed(self):
        oid, tid = _paid_order("newebpay")
        db = _Session()
        try:
            with pytest.raises(refund_svc.OrderRefundError):
                refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, newebpay_client=_FakeNeweb(status="FAIL"))
        finally:
            db.close()
        assert _get(oid).refund_status == "failed"


class TestGuardsAndCrashSafety:
    def test_processing_persisted_before_external_call(self):
        oid, tid = _paid_order("newebpay")
        observed = {}

        class _Obs:
            def refund(self, *, merchant_order_no, trade_no, amount_twd):
                s2 = _Session()
                try:
                    observed["s"] = s2.get(Order, oid).refund_status
                finally:
                    s2.close()
                return {"Status": "SUCCESS", "Message": "m", "Result": {}}

        db = _Session()
        try:
            refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, newebpay_client=_Obs())
        finally:
            db.close()
        assert observed["s"] == "processing"

    def test_unpaid_rejected(self):
        db = _Session()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="pro")
            db.add(t)
            db.flush()
            o = Order(tenant_id=t.id, status="pending", total_cents=1000)
            db.add(o)
            db.commit()
            with pytest.raises(refund_svc.OrderRefundError):
                refund_svc.request_order_refund(db, tenant_id=t.id, order_id=o.id, actor_user_id=1)
        finally:
            db.close()

    def test_over_refund_rejected(self):
        oid, tid = _paid_order("stub", total=10000)
        db = _Session()
        try:
            with pytest.raises(refund_svc.OrderRefundError):
                refund_svc.request_order_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, amount_cents=20000)
        finally:
            db.close()

    def test_missing_provider_manual(self):
        db = _Session()
        try:
            t = Tenant(name=f"t_{uuid.uuid4().hex[:6]}", plan="pro")
            db.add(t)
            db.flush()
            o = Order(tenant_id=t.id, status=ORDER_PAID, total_cents=10000, payment_provider=None)
            db.add(o)
            db.commit()
            with pytest.raises(refund_svc.OrderRefundError):
                refund_svc.request_order_refund(db, tenant_id=t.id, order_id=o.id, actor_user_id=1)
            assert db.get(Order, o.id).refund_status == "manual_required"
        finally:
            db.close()


class TestManualConfirm:
    def test_manual_full(self):
        oid, tid = _paid_order("ecpay")
        db = _Session()
        try:
            o = refund_svc.confirm_manual_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, note="退現金")
            assert o.refund_status == "refunded" and o.refund_provider_code == "MANUAL"
        finally:
            db.close()

    def test_manual_blocked_while_processing(self):
        """A3 審查:auto 退款進行中(PROCESSING)不可人工對帳 → 防雙重退款。"""
        oid, tid = _paid_order("linepay")
        db = _Session()
        try:
            o = db.get(Order, oid)
            o.refund_status = "processing"
            db.commit()
            with pytest.raises(refund_svc.OrderRefundError):
                refund_svc.confirm_manual_refund(db, tenant_id=tid, order_id=oid, actor_user_id=1, note="外部退款")
        finally:
            db.close()
        assert _get(oid).refund_status == "processing"  # 未被覆寫


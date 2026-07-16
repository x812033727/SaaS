"""R2-3 測試 — C2 電子發票 + C4 定金。"""

from __future__ import annotations

import datetime
import os
import uuid
from types import SimpleNamespace

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
from saas_mvp.services import platform_payment_config as payment_config_svc  # noqa: E402
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

    def test_ecpay_void_payload_encrypts(self):
        captured = {}

        def fake_post(url, body):
            import json

            captured["url"] = url
            envelope = json.loads(body)
            captured["data"] = aes_decrypt_data(envelope["Data"], _KEY, _IV)
            response_data = aes_encrypt_data(
                {
                    "RtnCode": "1",
                    "RtnMsg": "作廢發票成功",
                    "InvoiceNo": "AB12345678",
                },
                _KEY,
                _IV,
            )
            return json.dumps({"TransCode": "1", "Data": response_data})

        issuer = EcpayInvoiceIssuer(
            merchant_id="2000132",
            hash_key=_KEY,
            hash_iv=_IV,
            env="stage",
            http_post=fake_post,
        )
        result = issuer.void(
            invoice_no="AB12345678",
            invoice_date="2030-06-15",
            reason="退款",
        )
        assert result.invoice_no == "AB12345678"
        assert captured["data"] == {
            "MerchantID": "2000132",
            "InvoiceNo": "AB12345678",
            "InvoiceDate": "2030-06-15",
            "Reason": "退款",
        }
        assert captured["url"].endswith("/B2CInvoice/Invalid")


class TestVoidInvoice:
    def _issued(self, db, *, status="issued", provider="stub"):
        tenant = _tenant(db)
        row = Invoice(
            tenant_id=tenant.id,
            relate_number=f"SCVOID{uuid.uuid4().hex[:8]}",
            invoice_no="ST12345678",
            invoice_date="2030-06-15",
            amount_cents=89900,
            buyer_email="owner@example.com",
            status=status,
            provider=provider,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def test_success_and_idempotent(self, db):
        row = self._issued(db)
        issuer = StubInvoiceIssuer()
        result = invoices_svc.void_invoice(
            db, row.id, reason="訂單退款", issuer=issuer
        )
        assert result.status == "void"
        assert result.void_reason == "訂單退款"
        assert result.voided_at is not None
        assert result.void_error_msg is None
        again = invoices_svc.void_invoice(
            db, row.id, reason="訂單退款", issuer=issuer
        )
        assert again.status == "void"
        assert len(issuer.voided) == 1

    def test_provider_failure_keeps_invoice_issued(self, db):
        class Boom(StubInvoiceIssuer):
            def void(self, **kwargs):
                raise InvoiceError("allowance exists")

        row = self._issued(db)
        with pytest.raises(invoices_svc.InvoiceProviderError, match="綠界拒絕作廢"):
            invoices_svc.void_invoice(db, row.id, reason="退款", issuer=Boom())
        db.refresh(row)
        assert row.status == "issued"
        assert "allowance exists" in row.void_error_msg
        assert row.voided_at is None

    @pytest.mark.parametrize("reason", ["", "太" * 21])
    def test_reason_required_and_limited(self, db, reason):
        row = self._issued(db)
        with pytest.raises(invoices_svc.InvoiceOperationError, match="1–20"):
            invoices_svc.void_invoice(db, row.id, reason=reason)
        db.refresh(row)
        assert row.status == "issued"

    def test_only_issued_invoice_can_be_voided(self, db):
        row = self._issued(db, status="failed")
        with pytest.raises(invoices_svc.InvoiceOperationError, match="只有已開立"):
            invoices_svc.void_invoice(db, row.id, reason="退款")


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

    def _paid_cancelled(self, db, *, provider="stub", payment_type="stub"):
        tenant, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=tenant.id, slot_id=slot_id, party_size=1,
            line_user_id=f"U{uuid.uuid4().hex[:8]}",
        )
        deposit_svc.mark_paid(
            db,
            resv,
            provider=provider,
            provider_merchant_id="3000007" if provider == "ecpay" else None,
            provider_trade_no="2401010000000001" if provider == "ecpay" else None,
            payment_type=payment_type,
        )
        booking_svc.cancel_reservation(
            db, tenant_id=tenant.id, reservation_id=resv.id
        )
        return tenant, resv

    def test_stub_full_refund_is_idempotent(self, db):
        tenant, resv = self._paid_cancelled(db)
        refunded = deposit_svc.request_full_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id, actor_user_id=1
        )
        assert refunded.deposit_status == "refunded"
        assert refunded.deposit_refund_status == "refunded"
        assert refunded.deposit_refund_attempts == 1
        again = deposit_svc.request_full_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id, actor_user_id=1
        )
        assert again.deposit_refund_attempts == 1

    def test_ecpay_refund_success_and_explicit_failure_retry(self, db, monkeypatch):
        monkeypatch.setattr(
            payment_config_svc,
            "effective_payment_config",
            lambda *_: SimpleNamespace(
                provider="ecpay", environment="prod", merchant_id="3000007"
            ),
        )
        tenant, resv = self._paid_cancelled(
            db, provider="ecpay", payment_type="Credit_CreditCard"
        )

        class FakeClient:
            responses = [
                {"RtnCode": "0", "RtnMsg": "尚未關帳"},
                {"RtnCode": "1", "RtnMsg": "Success"},
            ]

            def refund_credit(self, **kwargs):
                assert kwargs["amount_twd"] == 200
                assert kwargs["trade_no"] == "2401010000000001"
                return self.responses.pop(0)

        fake = FakeClient()
        with pytest.raises(deposit_svc.DepositRefundError, match="尚未關帳"):
            deposit_svc.request_full_refund(
                db, tenant_id=tenant.id, reservation_id=resv.id,
                actor_user_id=1, ecpay_client=fake,
            )
        db.refresh(resv)
        assert resv.deposit_refund_status == "failed"

        deposit_svc.request_full_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id,
            actor_user_id=1, ecpay_client=fake,
        )
        db.refresh(resv)
        assert resv.deposit_status == "refunded"
        assert resv.deposit_refund_attempts == 2

    def test_ambiguous_network_failure_requires_manual_confirmation(self, db, monkeypatch):
        monkeypatch.setattr(
            payment_config_svc,
            "effective_payment_config",
            lambda *_: SimpleNamespace(
                provider="ecpay", environment="prod", merchant_id="3000007"
            ),
        )
        tenant, resv = self._paid_cancelled(
            db, provider="ecpay", payment_type="Credit_CreditCard"
        )

        class TimeoutClient:
            calls = 0

            def refund_credit(self, **_kwargs):
                self.calls += 1
                raise TimeoutError("unknown provider result")

        fake = TimeoutClient()
        with pytest.raises(deposit_svc.DepositRefundError, match="結果不確定"):
            deposit_svc.request_full_refund(
                db, tenant_id=tenant.id, reservation_id=resv.id,
                actor_user_id=1, ecpay_client=fake,
            )
        with pytest.raises(deposit_svc.DepositRefundError, match="不能自動重送"):
            deposit_svc.request_full_refund(
                db, tenant_id=tenant.id, reservation_id=resv.id,
                actor_user_id=1, ecpay_client=fake,
            )
        assert fake.calls == 1

        deposit_svc.confirm_manual_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id,
            actor_user_id=1, note="綠界後台退款單 RF12345",
        )
        db.refresh(resv)
        assert resv.deposit_status == "refunded"
        assert resv.deposit_refund_provider_code == "MANUAL"

    # ── R3-A3:部分退款 + 退款通知 ────────────────────────────────────────────

    def _refund_notifications(self, db, resv_id):
        from saas_mvp.models.booking_notification import (
            NOTIFY_REFUND,
            BookingNotification,
        )

        return db.execute(
            select(BookingNotification).where(
                BookingNotification.reservation_id == resv_id,
                BookingNotification.kind == NOTIFY_REFUND,
            )
        ).scalars().all()

    def test_partial_refund_records_amount_and_notifies(self, db):
        tenant, resv = self._paid_cancelled(db)  # 定金 NT$200
        refunded = deposit_svc.request_full_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id,
            actor_user_id=1, amount_cents=10000,
        )
        assert refunded.deposit_status == "refunded"
        assert refunded.deposit_refunded_cents == 10000
        rows = self._refund_notifications(db, resv.id)
        assert len(rows) == 1
        assert "部分退款 NT$100" in rows[0].payload_text
        assert "NT$200" in rows[0].payload_text

    def test_full_refund_notifies_full_text(self, db):
        tenant, resv = self._paid_cancelled(db)
        deposit_svc.request_full_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id, actor_user_id=1
        )
        db.refresh(resv)
        assert resv.deposit_refunded_cents == 20000
        rows = self._refund_notifications(db, resv.id)
        assert len(rows) == 1 and "全額退款" in rows[0].payload_text

    def test_partial_refund_amount_validation(self, db):
        tenant, resv = self._paid_cancelled(db)
        for bad in (0, -100, 20100, 150):  # 0/負數/超額/非整數元
            with pytest.raises(deposit_svc.DepositRefundError):
                deposit_svc.request_full_refund(
                    db, tenant_id=tenant.id, reservation_id=resv.id,
                    actor_user_id=1, amount_cents=bad,
                )
        db.refresh(resv)
        # 驗證失敗不落任何狀態(仍可正常退款)
        assert resv.deposit_status == "paid"
        assert resv.deposit_refund_attempts == 0

    def test_ecpay_partial_refund_passes_amount(self, db, monkeypatch):
        monkeypatch.setattr(
            payment_config_svc,
            "effective_payment_config",
            lambda *_: SimpleNamespace(
                provider="ecpay", environment="prod", merchant_id="3000007"
            ),
        )
        tenant, resv = self._paid_cancelled(
            db, provider="ecpay", payment_type="Credit_CreditCard"
        )

        class FakeClient:
            def refund_credit(self, **kwargs):
                assert kwargs["amount_twd"] == 50  # 部分金額傳給綠界
                return {"RtnCode": "1", "RtnMsg": "Success"}

        deposit_svc.request_full_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id,
            actor_user_id=1, amount_cents=5000, ecpay_client=FakeClient(),
        )
        db.refresh(resv)
        assert resv.deposit_status == "refunded"
        assert resv.deposit_refunded_cents == 5000

    def test_manual_confirm_partial_amount(self, db, monkeypatch):
        monkeypatch.setattr(
            payment_config_svc,
            "effective_payment_config",
            lambda *_: SimpleNamespace(
                provider="ecpay", environment="prod", merchant_id="3000007"
            ),
        )
        tenant, resv = self._paid_cancelled(
            db, provider="ecpay", payment_type="Credit_CreditCard"
        )

        class TimeoutClient:
            def refund_credit(self, **_kwargs):
                raise TimeoutError("unknown provider result")

        with pytest.raises(deposit_svc.DepositRefundError):
            deposit_svc.request_full_refund(
                db, tenant_id=tenant.id, reservation_id=resv.id,
                actor_user_id=1, ecpay_client=TimeoutClient(),
            )
        deposit_svc.confirm_manual_refund(
            db, tenant_id=tenant.id, reservation_id=resv.id,
            actor_user_id=1, note="綠界後台退款單 RF999", amount_cents=8000,
        )
        db.refresh(resv)
        assert resv.deposit_refunded_cents == 8000
        rows = self._refund_notifications(db, resv.id)
        assert len(rows) == 1 and "部分退款 NT$80" in rows[0].payload_text

    def test_non_credit_payment_never_calls_credit_refund_api(self, db):
        tenant, resv = self._paid_cancelled(
            db, provider="ecpay", payment_type="ATM_TAISHIN"
        )
        with pytest.raises(deposit_svc.DepositRefundError, match="不是信用卡"):
            deposit_svc.request_full_refund(
                db, tenant_id=tenant.id, reservation_id=resv.id,
                actor_user_id=1,
            )
        assert resv.deposit_refund_status == "manual_required"

    def test_refund_requires_cancelled_booking_and_tenant_scope(self, db):
        tenant, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=tenant.id, slot_id=slot_id, party_size=1,
            line_user_id="Uscope",
        )
        deposit_svc.mark_paid(db, resv, provider="stub", payment_type="stub")
        with pytest.raises(deposit_svc.DepositRefundError, match="請先取消"):
            deposit_svc.request_full_refund(
                db, tenant_id=tenant.id, reservation_id=resv.id, actor_user_id=1
            )
        with pytest.raises(deposit_svc.DepositRefundError, match="不存在"):
            deposit_svc.request_full_refund(
                db, tenant_id=tenant.id + 999, reservation_id=resv.id,
                actor_user_id=1,
            )

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

    def test_concurrent_paid_not_clobbered(self, db, monkeypatch):
        """列出待過期後、逐筆處理前若付款回調搶先標 PAID,狀態守衛 UPDATE 必須
        略過該筆(rowcount=0)而非把 PAID 覆寫成 EXPIRED / 取消已付款預約。"""
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1, line_user_id="Urace",
        )
        resv.deposit_expires_at = _NOW - datetime.timedelta(minutes=1)
        db.commit()
        rid = resv.id

        factory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        real_list = deposit_svc.list_expired_pending

        def racing_list(dbx, **kw):
            rows = real_list(dbx, **kw)  # 仍是 pending → 進入 targets
            # 模擬併發:列出後、處理前,付款回調搶先標 PAID 並提交
            r = dbx.get(Reservation, rid)
            deposit_svc.mark_paid(dbx, r)
            dbx.commit()
            return rows

        monkeypatch.setattr(deposit_svc, "list_expired_pending", racing_list)
        results = cancel_unpaid_deposits(
            session_factory=factory, push_client=FakeLinePushClient(),
            apply=True, now=_NOW,
        )
        assert [r.status for r in results] == ["skipped"]  # 未取消
        db.expire_all()
        r2 = db.get(Reservation, rid)
        assert r2.deposit_status == "paid"              # PAID 未被 clobber
        assert r2.status != RESERVATION_CANCELLED       # 已付款預約未被取消


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
        tno = resv.deposit_merchant_trade_no
        db.close()

        r = client.get(f"/payments/ecpay/deposit/{tno}")
        assert r.status_code == 200 and "模擬定金付款" in r.text
        r = client.post(f"/payments/stub/deposit-paid/{tno}")
        assert "已付款" in r.text
        db = _Session()
        try:
            paid = db.get(Reservation, rid)
            assert paid.deposit_status == "paid"
            assert paid.deposit_provider == "stub"
            assert paid.deposit_payment_type == "stub"
        finally:
            db.close()

    def test_ecpay_callback_saves_refund_transaction_snapshot(self, client):
        db = _Session()
        tenant, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=tenant.id, slot_id=slot_id, party_size=1,
            line_user_id="Uecpayrefund",
        )
        rid = resv.id
        params = {
            "MerchantTradeNo": resv.deposit_merchant_trade_no,
            "TradeNo": "2401010000009999",
            "TradeAmt": "200",
            "PaymentType": "Credit_CreditCard",
            "RtnCode": "1",
        }
        from saas_mvp.services.payment_ecpay import get_ecpay_client

        params["CheckMacValue"] = get_ecpay_client(db).check_mac_value(params)
        db.close()

        response = client.post("/payments/ecpay/deposit-callback", data=params)
        assert response.text == "1|OK"
        with _Session() as verify_db:
            paid = verify_db.get(Reservation, rid)
            assert paid.deposit_provider == "ecpay"
            assert paid.deposit_provider_trade_no == "2401010000009999"
            assert paid.deposit_payment_type == "Credit_CreditCard"

    def test_paid_page_shows_confirmed(self, client):
        db = _Session()
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1,
            line_user_id="Uweb2",
        )
        deposit_svc.mark_paid(db, resv)
        tno = resv.deposit_merchant_trade_no
        db.close()
        r = client.get(f"/payments/ecpay/deposit/{tno}")
        assert "已付款" in r.text

    def test_nonecpay_real_provider_no_free_deposit(self, client, monkeypatch):
        """非 ecpay 的真 provider(newebpay/linepay)沒有定金後端:定金頁不得退化成
        免費模擬頁,模擬付款端點必須 403 —— 否則等於公開的免費定金繞過。"""
        monkeypatch.setattr(settings, "payment_provider", "newebpay")
        db = _Session()
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1, line_user_id="Uweb3",
        )
        rid = resv.id
        tno = resv.deposit_merchant_trade_no
        db.close()

        r = client.get(f"/payments/ecpay/deposit/{tno}")
        assert r.status_code == 503
        assert "模擬付款成功" not in r.text
        r2 = client.post(f"/payments/stub/deposit-paid/{tno}")
        assert r2.status_code == 403
        db = _Session()
        try:
            assert db.get(Reservation, rid).deposit_status == "pending"  # 仍未付款
        finally:
            db.close()

    def test_deposit_url_keyed_by_unguessable_trade_no(self, client):
        """PEA-1/PEA-2:定金端點以 trade_no 為鍵,枚舉 reservation_id 無法命中
        (未知 trade_no 一律 404),且 trade_no 具足夠隨機熵。"""
        db = _Session()
        t, slot_id = _deposit_tenant(db)
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot_id, party_size=1, line_user_id="Uweb4",
        )
        rid = resv.id
        tno = resv.deposit_merchant_trade_no
        db.close()
        # 用 reservation_id 當 URL 猜不到(被當成 trade_no 字串,查無)→ 404
        assert client.get(f"/payments/ecpay/deposit/{rid}").status_code == 404
        assert client.post(f"/payments/stub/deposit-paid/{rid}").status_code == 404
        # 未知 trade_no 也一律 404(無枚舉/竊改)
        assert client.post("/payments/stub/deposit-paid/DPZZZZZZZZ99").status_code == 404
        assert client.get("/payments/ecpay/deposit/DPZZZZZZZZ99").status_code == 404
        # 正確 trade_no 才可用;且 trade_no 有足夠隨機段(非僅 id + 8-bit)
        assert client.get(f"/payments/ecpay/deposit/{tno}").status_code == 200
        assert tno.startswith("DP") and len(tno) >= 14

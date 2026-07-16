"""R3-A2 測試 — 定金多 provider(藍新 MPG + LINE Pay 一次性)+ provider 中立入口。"""

from __future__ import annotations

import datetime
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.config import settings  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import booking as booking_svc  # noqa: E402
from saas_mvp.services import deposit as deposit_svc  # noqa: E402
from saas_mvp.services.payment_newebpay import NewebPayClient  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_NOW = datetime.datetime(2030, 6, 15, 9, 0, tzinfo=datetime.timezone.utc)
_HK = "k" * 32
_HIV = "i" * 16


def _deposit_reservation() -> tuple[int, str]:
    """建含定金快照的預約,回 (reservation_id, deposit trade_no)。"""
    db = _Session()
    try:
        # plan="pro" → DEPOSIT_PAYMENT 經方案 bundle 啟用(同 test_invoices_deposit)
        t = Tenant(
            name=f"dp_{uuid.uuid4().hex[:8]}", plan="pro",
            deposit_cents=20000, deposit_hold_minutes=30,
        )
        db.add(t)
        db.flush()
        slot = BookingSlot(
            tenant_id=t.id,
            slot_start=_NOW + datetime.timedelta(days=1),
            max_capacity=4,
        )
        db.add(slot)
        db.commit()
        resv = booking_svc.book_slot(
            db, tenant_id=t.id, slot_id=slot.id, party_size=1,
            line_user_id=f"U{uuid.uuid4().hex[:8]}",
        )
        assert resv.deposit_status == "pending"
        return resv.id, resv.deposit_merchant_trade_no
    finally:
        db.close()


def _resv(rid: int) -> Reservation:
    db = _Session()
    try:
        return db.get(Reservation, rid)
    finally:
        db.close()


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
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def newebpay(monkeypatch):
    monkeypatch.setattr(settings, "payment_provider", "newebpay")
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
    monkeypatch.setattr(settings, "newebpay_merchant_id", "MS123456")
    monkeypatch.setattr(settings, "newebpay_hash_key", _HK)
    monkeypatch.setattr(settings, "newebpay_hash_iv", _HIV)


@pytest.fixture()
def linepay(monkeypatch):
    monkeypatch.setattr(settings, "payment_provider", "linepay")
    monkeypatch.setattr(settings, "public_base_url", "https://shop.example")
    monkeypatch.setattr(settings, "line_pay_channel_id", "chan")
    monkeypatch.setattr(settings, "line_pay_channel_secret", "sec")


class TestNeutralEntry:
    def test_payment_url_uses_neutral_path(self, client):
        rid, tno = _deposit_reservation()
        url = deposit_svc.payment_url(_resv(rid))
        assert url.endswith(f"/payments/deposit/{tno}")

    def test_neutral_and_legacy_alias_both_serve_stub_page(self, client):
        """新入口與舊 /payments/ecpay/deposit/ alias 行為一致(已寄出連結不斷)。"""
        _, tno = _deposit_reservation()
        for path in (f"/payments/deposit/{tno}", f"/payments/ecpay/deposit/{tno}"):
            r = client.get(path)
            assert r.status_code == 200 and "模擬定金付款" in r.text

    def test_unknown_trade_no_404(self, client):
        assert client.get("/payments/deposit/DPZZZZZZZZ99").status_code == 404


class TestNewebPayDeposit:
    def _notify_params(self, trade_no, amt, status="SUCCESS"):
        """組真實藍新 JSON-with-nested-Result 回調 wire format(同 test_payment_newebpay)。"""
        from cryptography.hazmat.primitives import padding as _pad
        from cryptography.hazmat.primitives.ciphers import (
            Cipher as _Cipher,
            algorithms as _alg,
            modes as _modes,
        )

        c = NewebPayClient(merchant_id="MS123456", hash_key=_HK, hash_iv=_HIV, env="stage")
        payload = {
            "Status": status,
            "Result": {
                "MerchantOrderNo": trade_no,
                "Amt": int(amt),
                "TradeNo": "24010112000012345",
                "PaymentType": "CREDIT",
            },
        }
        plain = json.dumps(payload).encode("utf-8")
        key, iv = c.hash_key.encode(), c.hash_iv.encode()
        padder = _pad.PKCS7(_alg.AES.block_size).padder()
        padded = padder.update(plain) + padder.finalize()
        enc = _Cipher(_alg.AES(key), _modes.CBC(iv)).encryptor()
        ti = (enc.update(padded) + enc.finalize()).hex()
        return {"TradeInfo": ti, "TradeSha": c.trade_sha(ti), "Status": status}

    def test_checkout_renders_mpg_form(self, client, newebpay):
        _, tno = _deposit_reservation()
        r = client.get(f"/payments/deposit/{tno}")
        assert r.status_code == 200
        assert "newebpay.com" in r.text
        assert "TradeInfo" in r.text and "TradeSha" in r.text

    def test_checkout_503_without_credentials(self, client, monkeypatch):
        monkeypatch.setattr(settings, "payment_provider", "newebpay")
        monkeypatch.setattr(settings, "newebpay_merchant_id", "")
        _, tno = _deposit_reservation()
        r = client.get(f"/payments/deposit/{tno}")
        assert r.status_code == 503
        assert "模擬付款成功" not in r.text  # 絕不退化成免費模擬頁

    def test_notify_marks_paid_with_snapshot_and_idempotent(self, client, newebpay):
        rid, tno = _deposit_reservation()
        params = self._notify_params(tno, 200)
        r = client.post("/payments/newebpay/deposit-notify", data=params)
        assert r.text == "1|OK"
        paid = _resv(rid)
        assert paid.deposit_status == "paid"
        assert paid.deposit_provider == "newebpay"
        assert paid.deposit_provider_trade_no == "24010112000012345"
        assert paid.deposit_payment_type == "CREDIT"
        # 重送冪等
        r2 = client.post("/payments/newebpay/deposit-notify", data=params)
        assert r2.text == "1|OK" and _resv(rid).deposit_status == "paid"

    def test_notify_bad_sha_rejected(self, client, newebpay):
        rid, tno = _deposit_reservation()
        params = self._notify_params(tno, 200)
        params["TradeSha"] = "DEADBEEF"
        r = client.post("/payments/newebpay/deposit-notify", data=params)
        assert r.text.startswith("0|")
        assert _resv(rid).deposit_status == "pending"

    def test_notify_amount_mismatch_rejected(self, client, newebpay):
        rid, tno = _deposit_reservation()
        r = client.post(
            "/payments/newebpay/deposit-notify", data=self._notify_params(tno, 999)
        )
        assert r.text == "0|amount mismatch"
        assert _resv(rid).deposit_status == "pending"

    def test_notify_failed_status_keeps_pending(self, client, newebpay):
        rid, tno = _deposit_reservation()
        r = client.post(
            "/payments/newebpay/deposit-notify",
            data=self._notify_params(tno, 200, status="FAIL"),
        )
        assert r.text == "1|OK"
        assert _resv(rid).deposit_status == "pending"


class TestLinePayDeposit:
    def _mock_post(self, monkeypatch, responses):
        import saas_mvp.services.payment_linepay as lp

        calls = []

        def fake_post(url, body, headers):
            calls.append({"url": url, "body": json.loads(body)})
            return json.dumps(responses.pop(0))

        monkeypatch.setattr(lp, "_urllib_post", fake_post)
        return calls

    def test_checkout_redirects_and_binds_txid(self, client, linepay, monkeypatch):
        calls = self._mock_post(monkeypatch, [{
            "returnCode": "0000",
            "info": {"transactionId": 987654321,
                     "paymentUrl": {"web": "https://sandbox-web-pay.line.me/x"}},
        }])
        rid, tno = _deposit_reservation()
        r = client.get(f"/payments/deposit/{tno}", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "https://sandbox-web-pay.line.me/x"
        assert _resv(rid).deposit_payment_txn_id == "987654321"
        # 金額以 DB deposit_cents 為準、orderId 用 trade_no
        assert calls[0]["body"]["amount"] == 200
        assert calls[0]["body"]["orderId"] == tno

    def test_checkout_api_failure_friendly_502(self, client, linepay, monkeypatch):
        self._mock_post(monkeypatch, [{"returnCode": "1104", "returnMessage": "bad"}])
        _, tno = _deposit_reservation()
        r = client.get(f"/payments/deposit/{tno}", follow_redirects=False)
        assert r.status_code == 502
        assert "暫時無法使用" in r.text

    def _paid_setup(self, txn_id="987654321"):
        rid, tno = _deposit_reservation()
        db = _Session()
        try:
            resv = db.get(Reservation, rid)
            resv.deposit_payment_txn_id = txn_id
            db.commit()
        finally:
            db.close()
        return rid, tno

    def test_confirm_marks_paid_with_snapshot(self, client, linepay, monkeypatch):
        self._mock_post(monkeypatch, [{"returnCode": "0000", "info": {}}])
        rid, tno = self._paid_setup()
        r = client.get(
            f"/payments/linepay/deposit-confirm?transactionId=987654321&orderId={tno}"
        )
        assert "定金已付款" in r.text
        paid = _resv(rid)
        assert paid.deposit_status == "paid"
        assert paid.deposit_provider == "linepay"
        assert paid.deposit_provider_trade_no == "987654321"

    def test_confirm_txid_mismatch_400(self, client, linepay):
        rid, tno = self._paid_setup(txn_id="987654321")
        r = client.get(
            f"/payments/linepay/deposit-confirm?transactionId=42&orderId={tno}"
        )
        assert r.status_code == 400
        assert _resv(rid).deposit_status == "pending"

    def test_confirm_already_paid_short_circuit(self, client, linepay):
        rid, tno = self._paid_setup()
        db = _Session()
        try:
            deposit_svc.mark_paid(db, db.get(Reservation, rid), provider="linepay")
        finally:
            db.close()
        r = client.get(
            f"/payments/linepay/deposit-confirm?transactionId=987654321&orderId={tno}"
        )
        assert "定金已付款" in r.text  # 不打 API 直接成功頁

    def test_confirm_unknown_order_404(self, client, linepay):
        r = client.get(
            "/payments/linepay/deposit-confirm?transactionId=42&orderId=DPZZZZ"
        )
        assert r.status_code == 404

    def test_cancel_page(self, client):
        r = client.get("/payments/linepay/deposit-cancel")
        assert "已取消付款" in r.text

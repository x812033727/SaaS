"""店家發票資料：自助設定、加密、快照與綠界欄位規則。"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.audit_log import AuditLog
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.tenant_invoice_profile import TenantInvoiceProfile
from saas_mvp.models.user import User
from saas_mvp.services import features as features_svc
from saas_mvp.services import invoice_profiles as profiles_svc
from saas_mvp.services import invoices as invoices_svc
from saas_mvp.services import subscriptions as subscriptions_svc
from saas_mvp.services.invoice_ecpay import (
    EcpayInvoiceIssuer,
    InvoiceError,
    StubInvoiceIssuer,
    aes_decrypt_data,
    aes_encrypt_data,
)

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_KEY = "a" * 16
_IV = "b" * 16


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        with _Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    with _Session() as session:
        yield session


def _login(client: TestClient, *, role: str = "owner") -> tuple[int, int]:
    email = f"invoice_profile_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"Invoice Profile {uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        user.role = role
        db.commit()
        ids = (user.id, user.tenant_id)
    response = client.post(
        "/ui/login", data={"email": email, "password": "Test1234!"}
    )
    assert response.status_code == 303
    return ids


def _profile_form(**overrides) -> dict[str, str]:
    data = {
        "mode": "personal",
        "buyer_name": "",
        "buyer_identifier": "",
        "carrier_type": "ecpay",
        "carrier_number": "",
        "donation_code": "",
    }
    data.update(overrides)
    return data


def _tenant_and_charge(db):
    tenant = Tenant(name=f"profile_{uuid.uuid4().hex[:8]}", plan="pro")
    db.add(tenant)
    db.flush()
    owner = User(
        email=f"{tenant.name}@example.com",
        hashed_password="x",
        tenant_id=tenant.id,
        role="owner",
    )
    db.add(owner)
    db.commit()
    subscription = subscriptions_svc.create_subscription(
        db,
        tenant_id=tenant.id,
        feature=features_svc.BUNDLE_PRO,
        amount_cents=89900,
    )
    subscriptions_svc.activate(db, subscription)
    from saas_mvp.models.subscription_charge import SubscriptionCharge

    charge = db.execute(
        select(SubscriptionCharge).where(
            SubscriptionCharge.subscription_id == subscription.id
        )
    ).scalar_one()
    return tenant, owner, charge


def test_owner_can_save_encrypted_mobile_carrier_and_page_masks_it(client):
    user_id, tenant_id = _login(client)
    carrier = "/AB12+-."
    response = client.post(
        "/ui/billing/invoice-profile",
        data=_profile_form(carrier_type="mobile", carrier_number=carrier.lower()),
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("invoice_profile_saved=1")
    with _Session() as db:
        row = db.query(TenantInvoiceProfile).filter_by(tenant_id=tenant_id).one()
        assert row.carrier_number == carrier
        assert carrier.encode() not in row.carrier_number_enc
        assert row.updated_by_user_id == user_id
        audit = db.query(AuditLog).filter_by(
            action="billing.invoice_profile.update"
        ).one()
        assert "carrier_number" not in (audit.detail_json or "")
    page = client.get("/ui/billing")
    assert page.status_code == 200
    assert "電子發票資料" in page.text
    assert "載具號碼加密保存" in page.text
    assert carrier not in page.text
    assert "docker-compose" not in page.text and ".env" not in page.text


def test_blank_carrier_preserves_existing_encrypted_value(client):
    _, tenant_id = _login(client)
    carrier = "/AB12+-."
    assert client.post(
        "/ui/billing/invoice-profile",
        data=_profile_form(carrier_type="mobile", carrier_number=carrier),
    ).status_code == 303
    assert client.post(
        "/ui/billing/invoice-profile",
        data=_profile_form(carrier_type="mobile", carrier_number=""),
    ).status_code == 303
    with _Session() as db:
        row = db.query(TenantInvoiceProfile).filter_by(tenant_id=tenant_id).one()
        assert row.carrier_number == carrier


def test_staff_cannot_read_or_change_invoice_profile(client):
    _login(client, role="staff")
    assert client.get("/ui/billing").status_code == 403
    assert client.post(
        "/ui/billing/invoice-profile", data=_profile_form()
    ).status_code == 403


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            _profile_form(
                mode="business", buyer_name="公司", buyer_identifier="123"
            ),
            "統一編號必須為 8 碼數字且檢查碼正確",
        ),
        (
            _profile_form(
                mode="business", buyer_name="公司", buyer_identifier="12345678"
            ),
            "統一編號必須為 8 碼數字且檢查碼正確",
        ),
        (
            _profile_form(carrier_type="mobile", carrier_number="12345678"),
            "手機條碼必須為 / 加 7 碼",
        ),
        (
            _profile_form(carrier_type="citizen", carrier_number="AB123"),
            "自然人憑證載具必須為 2 碼大寫英文加 14 碼數字",
        ),
        (
            _profile_form(mode="donation", donation_code="12A"),
            "愛心捐贈碼必須為 3–7 碼數字",
        ),
    ],
)
def test_invalid_profile_is_rejected_without_persisting(client, data, message):
    _login(client)
    response = client.post("/ui/billing/invoice-profile", data=data)
    assert response.status_code == 400
    assert message in response.text
    with _Session() as db:
        assert db.query(TenantInvoiceProfile).count() == 0


def test_failed_invoice_retry_uses_original_encrypted_snapshot(db):
    class FailingIssuer(StubInvoiceIssuer):
        def issue(self, **kwargs):
            raise InvoiceError("temporary outage")

    tenant, owner, charge = _tenant_and_charge(db)
    profiles_svc.save_profile(
        db,
        tenant_id=tenant.id,
        mode="business",
        buyer_name="原始股份有限公司",
        buyer_identifier="97025978",
        carrier_type="mobile",
        carrier_number="/AB12+-.",
        donation_code="",
        actor_user_id=owner.id,
    )
    db.commit()
    invoice = invoices_svc.issue_for_charge(db, charge, issuer=FailingIssuer())
    assert invoice.status == "failed"
    assert invoice.carrier_number == "/AB12+-."
    assert b"/AB12+-." not in invoice.carrier_number_enc

    profiles_svc.save_profile(
        db,
        tenant_id=tenant.id,
        mode="donation",
        buyer_name="",
        buyer_identifier="",
        carrier_type="ecpay",
        carrier_number="",
        donation_code="168001",
        actor_user_id=owner.id,
    )
    db.commit()
    issuer = StubInvoiceIssuer()
    invoices_svc._attempt_issue(db, invoice, issuer=issuer)
    sent = issuer.issued[0]
    assert sent["buyer_name"] == "原始股份有限公司"
    assert sent["buyer_identifier"] == "97025978"
    assert sent["carrier_type"] == "mobile"
    assert sent["carrier_number"] == "/AB12+-."
    assert sent["donation_code"] == ""


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "buyer_name": "測試股份有限公司",
                "buyer_identifier": "97025978",
                "carrier_type": "mobile",
                "carrier_number": "/AB12+-.",
            },
            {
                "CustomerName": "測試股份有限公司",
                "CustomerIdentifier": "97025978",
                "Print": "0",
                "Donation": "0",
                "LoveCode": "",
                "CarrierType": "3",
                "CarrierNum": "/AB12+-.",
            },
        ),
        (
            {"donation_code": "168001"},
            {
                "CustomerName": "",
                "CustomerIdentifier": "",
                "Print": "0",
                "Donation": "1",
                "LoveCode": "168001",
                "CarrierType": "",
                "CarrierNum": "",
            },
        ),
    ],
)
def test_ecpay_payload_matches_business_carrier_and_donation_rules(kwargs, expected):
    captured = {}

    def fake_post(url, body):
        envelope = json.loads(body)
        captured.update(aes_decrypt_data(envelope["Data"], _KEY, _IV))
        result = aes_encrypt_data(
            {
                "RtnCode": "1",
                "InvoiceNo": "AB12345678",
                "InvoiceDate": "2030-06-15 12:00:00",
                "RandomNumber": "1234",
            },
            _KEY,
            _IV,
        )
        return json.dumps({"TransCode": "1", "Data": result})

    issuer = EcpayInvoiceIssuer(
        merchant_id="2000132",
        hash_key=_KEY,
        hash_iv=_IV,
        env="prod",
        http_post=fake_post,
    )
    issuer.issue(
        relate_number="SC1T99",
        amount_twd=899,
        buyer_email="owner@example.com",
        item_name="月費",
        **kwargs,
    )
    for key, value in expected.items():
        assert captured[key] == value


def test_stage_payload_replaces_real_buyer_pii_with_valid_test_values():
    captured = {}

    def fake_post(url, body):
        envelope = json.loads(body)
        captured.update(aes_decrypt_data(envelope["Data"], _KEY, _IV))
        result = aes_encrypt_data(
            {
                "RtnCode": "1",
                "InvoiceNo": "AB12345678",
                "InvoiceDate": "2030-06-15 12:00:00",
                "RandomNumber": "1234",
            },
            _KEY,
            _IV,
        )
        return json.dumps({"TransCode": "1", "Data": result})

    issuer = EcpayInvoiceIssuer(
        merchant_id="2000132",
        hash_key=_KEY,
        hash_iv=_IV,
        env="stage",
        http_post=fake_post,
    )
    issuer.issue(
        relate_number="SC2T99",
        amount_twd=899,
        buyer_email="real-person@example.com",
        buyer_name="真實公司名稱",
        buyer_identifier="12345678",
        carrier_type="mobile",
        carrier_number="/REAL123",
        item_name="月費",
    )
    assert captured["CustomerEmail"] == "test@ecpay.com.tw"
    assert captured["CustomerName"] == "綠界科技股份有限公司"
    assert captured["CustomerIdentifier"] == "97025978"
    assert captured["CarrierNum"] == "/ABC1234"
    assert "real-person" not in json.dumps(captured, ensure_ascii=False)
    assert "真實公司名稱" not in json.dumps(captured, ensure_ascii=False)


def test_invoice_profile_defaults_to_email_carrier(db):
    tenant, _, charge = _tenant_and_charge(db)
    issuer = StubInvoiceIssuer()
    invoice = invoices_svc.issue_for_charge(db, charge, issuer=issuer)
    assert invoice.tenant_id == tenant.id
    assert invoice.invoice_mode == "personal"
    assert issuer.issued[0]["carrier_type"] == "ecpay"

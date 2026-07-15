"""平台綠界後台設定：加密、動態生效、權限與扣款安全防呆。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db
from saas_mvp.models.audit_log import AuditLog
from saas_mvp.models.feature_subscription import SUB_ACTIVE, FeatureSubscription
from saas_mvp.models.platform_payment_config import PlatformPaymentConfig
from saas_mvp.models.reservation import RESERVATION_CANCELLED, Reservation
from saas_mvp.models.user import User
from saas_mvp.ops.check_readiness import run_checks
from saas_mvp.services import features as features_svc
from saas_mvp.services import platform_payment_config as service
from saas_mvp.services.billing import subscribe_feature
from saas_mvp.services.payment import get_payment_provider
from saas_mvp.services.payment_ecpay import EcpayPaymentProvider, get_ecpay_client

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_HASH_KEY = "5294y06JbISpM5x9"
_HASH_IV = "v77hoKGq4kWxNNIS"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "payment_provider", "stub")
    monkeypatch.setattr(settings, "public_base_url", "https://saas.example.com")
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        with _Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _login(client: TestClient, *, admin: bool) -> str:
    email = f"pay_admin_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"Pay {uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    if admin:
        with _Session() as db:
            user = db.query(User).filter_by(email=email).one()
            user.is_admin = True
            db.commit()
    response = client.post(
        "/ui/login", data={"email": email, "password": "Test1234!"}
    )
    assert response.status_code == 303
    return email


def _save(db, actor_id: int = 1, **overrides):
    values = {
        "merchant_id": "2000132",
        "hash_key": _HASH_KEY,
        "hash_iv": _HASH_IV,
        "environment": "stage",
        "actor_user_id": actor_id,
        "public_base_url": "https://saas.example.com",
    }
    values.update(overrides)
    return service.save_ecpay_config(db, **values)


def test_regular_user_cannot_manage_payment_settings(client):
    _login(client, admin=False)
    assert client.get("/ui/admin/payment-settings").status_code == 403
    response = client.post(
        "/ui/admin/payment-settings/ecpay",
        data={
            "merchant_id": "2000132",
            "hash_key": _HASH_KEY,
            "hash_iv": _HASH_IV,
            "environment": "stage",
        },
    )
    assert response.status_code == 403


def test_disabled_provider_blocks_direct_ecpay_checkout_pages(client):
    assert client.get("/payments/ecpay/checkout/999").status_code == 503
    assert client.get("/payments/ecpay/subscribe/999").status_code == 503


def test_admin_saves_encrypted_ecpay_and_all_factories_change_immediately(client):
    email = _login(client, admin=True)
    response = client.post(
        "/ui/admin/payment-settings/ecpay",
        data={
            "merchant_id": "2000132",
            "hash_key": _HASH_KEY,
            "hash_iv": _HASH_IV,
            "environment": "stage",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?saved=1")

    with _Session() as db:
        row = db.query(PlatformPaymentConfig).one()
        assert row.hash_key == _HASH_KEY
        assert row.hash_iv == _HASH_IV
        assert _HASH_KEY.encode() not in row.hash_key_enc
        assert _HASH_IV.encode() not in row.hash_iv_enc
        assert row.updated_by_user_id == db.query(User).filter_by(email=email).one().id
        assert isinstance(get_payment_provider(db), EcpayPaymentProvider)
        client_impl = get_ecpay_client(db)
        assert client_impl.merchant_id == "2000132"
        assert client_impl.hash_key == _HASH_KEY
        assert db.query(AuditLog).filter_by(
            action="platform.payment.ecpay.update"
        ).count() == 1

    page = client.get("/ui/admin/payment-settings")
    assert page.status_code == 200
    assert "綠界已啟用" in page.text
    assert "資料庫加密設定" in page.text
    assert _HASH_KEY not in page.text
    assert _HASH_IV not in page.text
    assert "不需修改 .env 或重啟" in page.text
    assert "https://saas.example.com/payments/ecpay/period-callback" in page.text


def test_backend_config_drives_recurring_subscription_not_stub(client):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        _save(db, actor_id=user.id)
        db.commit()
        result = subscribe_feature(
            db,
            user.tenant,
            features_svc.AUTO_REMINDER,
            actor_user_id=user.id,
        )
        assert result.mode == "ecpay"
        assert result.enabled is False
        assert result.checkout_url.startswith(
            "https://saas.example.com/payments/ecpay/subscribe/"
        )


def test_blank_secrets_preserve_existing_values(client):
    _login(client, admin=True)
    with _Session() as db:
        _save(db)
        db.commit()
    response = client.post(
        "/ui/admin/payment-settings/ecpay",
        data={
            "merchant_id": "3000007",
            "hash_key": "",
            "hash_iv": "",
            "environment": "stage",
        },
    )
    assert response.status_code == 303
    with _Session() as db:
        row = db.query(PlatformPaymentConfig).one()
        assert row.merchant_id == "3000007"
        assert row.hash_key == _HASH_KEY
        assert row.hash_iv == _HASH_IV


def test_production_rejects_public_test_merchant(client):
    _login(client, admin=True)
    response = client.post(
        "/ui/admin/payment-settings/ecpay",
        data={
            "merchant_id": "2000132",
            "hash_key": _HASH_KEY,
            "hash_iv": _HASH_IV,
            "environment": "prod",
        },
    )
    assert response.status_code == 400
    assert "正式環境不可使用" in response.text


def test_enabling_ecpay_requires_public_https_callback(client, monkeypatch):
    _login(client, admin=True)
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    response = client.post(
        "/ui/admin/payment-settings/ecpay",
        data={
            "merchant_id": "2000132",
            "hash_key": _HASH_KEY,
            "hash_iv": _HASH_IV,
            "environment": "stage",
        },
    )
    assert response.status_code == 400
    assert "HTTPS 對外網址" in response.text


def test_self_check_uses_encrypted_database_credentials(client):
    _login(client, admin=True)
    with _Session() as db:
        _save(db)
        db.commit()
    response = client.post("/ui/admin/payment-settings/check")
    assert response.status_code == 200
    assert "CheckMacValue 簽章自我檢查均通過" in response.text
    with _Session() as db:
        assert db.query(AuditLog).filter_by(
            action="platform.payment.ecpay.check"
        ).count() == 1


def test_unsettled_subscription_blocks_rotation_disable_and_reset(client):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        _save(db, actor_id=user.id)
        db.add(FeatureSubscription(
            tenant_id=user.tenant_id,
            feature=features_svc.AUTO_REMINDER,
            merchant_trade_no="SBUNSETTLED001",
            status=SUB_ACTIVE,
            period_amount_cents=20000,
        ))
        db.commit()

    rotate = client.post(
        "/ui/admin/payment-settings/ecpay",
        data={
            "merchant_id": "3000007",
            "hash_key": "anotherHashKey123",
            "hash_iv": "anotherHashIV1234",
            "environment": "stage",
        },
    )
    assert rotate.status_code == 400
    assert "仍有待付款、扣款中或停扣失敗" in rotate.text
    assert client.post("/ui/admin/payment-settings/disable").status_code == 409
    assert client.post("/ui/admin/payment-settings/reset").status_code == 409
    with _Session() as db:
        assert service.payment_provider(db, settings) == "ecpay"


def test_refundable_paid_deposit_blocks_credential_rotation(client):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        _save(db, actor_id=user.id)
        db.add(Reservation(
            tenant_id=user.tenant_id,
            slot_id=999999,
            party_size=1,
            status=RESERVATION_CANCELLED,
            deposit_cents=20000,
            deposit_status="paid",
            deposit_merchant_trade_no="DPLOCKCREDENTIAL001",
            deposit_provider="ecpay",
            deposit_provider_merchant_id="2000132",
            deposit_provider_trade_no="2401010000008888",
            deposit_payment_type="Credit_CreditCard",
        ))
        db.commit()

    rotate = client.post(
        "/ui/admin/payment-settings/ecpay",
        data={
            "merchant_id": "3000007",
            "hash_key": "anotherHashKey123",
            "hash_iv": "anotherHashIV1234",
            "environment": "stage",
        },
    )
    assert rotate.status_code == 400
    assert "尚未完成到場或退款的已付定金" in rotate.text
    assert client.post("/ui/admin/payment-settings/disable").status_code == 409
    assert client.post("/ui/admin/payment-settings/reset").status_code == 409


def test_disable_overrides_environment_ecpay_without_deleting_secrets(client, monkeypatch):
    _login(client, admin=True)
    monkeypatch.setattr(settings, "payment_provider", "ecpay")
    with _Session() as db:
        _save(db)
        db.commit()
    response = client.post("/ui/admin/payment-settings/disable")
    assert response.status_code == 303
    with _Session() as db:
        row = db.query(PlatformPaymentConfig).one()
        assert row.provider == "stub"
        assert row.hash_key == _HASH_KEY
        assert service.payment_provider(db, settings) == "stub"


def test_reset_returns_to_environment_fallback(client, monkeypatch):
    _login(client, admin=True)
    monkeypatch.setattr(settings, "payment_provider", "stub")
    with _Session() as db:
        _save(db)
        db.commit()
    response = client.post("/ui/admin/payment-settings/reset")
    assert response.status_code == 303
    with _Session() as db:
        assert db.query(PlatformPaymentConfig).count() == 0
        assert service.payment_provider(db, settings) == "stub"


def test_readiness_recognizes_backend_ecpay(client, monkeypatch):
    _login(client, admin=True)
    monkeypatch.setattr(settings, "payment_provider", "stub")
    with _Session() as db:
        _save(db)
        db.commit()
    checks = {item.name: item for item in run_checks(session_factory=_Session)}
    assert checks["payment"].status == "WARN"
    assert "source=database" in checks["payment"].detail

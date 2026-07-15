"""平台電子發票後台設定：權限、加密、動態 issuer 與營運防呆。"""

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
from saas_mvp.models.invoice import Invoice
from saas_mvp.models.platform_invoice_config import PlatformInvoiceConfig
from saas_mvp.models.user import User
from saas_mvp.ops.check_readiness import run_checks
from saas_mvp.services import platform_invoice_config as service
from saas_mvp.services.invoice_ecpay import EcpayInvoiceIssuer, get_invoice_issuer

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
    monkeypatch.setattr(settings, "invoice_provider", "stub")
    monkeypatch.setattr(settings, "ecpay_invoice_merchant_id", "")
    monkeypatch.setattr(settings, "ecpay_invoice_hash_key", "")
    monkeypatch.setattr(settings, "ecpay_invoice_hash_iv", "")
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
    email = f"invoice_{uuid.uuid4().hex[:8]}@example.com"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "Test1234!",
            "tenant_name": f"Invoice {uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    if admin:
        with _Session() as db:
            user = db.query(User).filter_by(email=email).one()
            user.is_admin = True
            db.commit()
    assert client.post(
        "/ui/login", data={"email": email, "password": "Test1234!"}
    ).status_code == 303
    return email


def _save(db, actor_id: int = 1, **overrides):
    values = {
        "merchant_id": "2000132",
        "hash_key": _HASH_KEY,
        "hash_iv": _HASH_IV,
        "environment": "stage",
        "actor_user_id": actor_id,
    }
    values.update(overrides)
    return service.save_ecpay_config(db, **values)


def test_regular_user_cannot_manage_invoice_settings(client):
    _login(client, admin=False)
    assert client.get("/ui/admin/invoice-settings").status_code == 403
    assert client.post(
        "/ui/admin/invoice-settings/ecpay",
        data={
            "merchant_id": "2000132",
            "hash_key": _HASH_KEY,
            "hash_iv": _HASH_IV,
            "environment": "stage",
        },
    ).status_code == 403
    assert client.post(
        "/ui/admin/invoice-settings/1/void", data={"reason": "退款"}
    ).status_code == 403


def test_admin_saves_encrypted_config_and_factory_changes_immediately(client):
    email = _login(client, admin=True)
    response = client.post(
        "/ui/admin/invoice-settings/ecpay",
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
        row = db.query(PlatformInvoiceConfig).one()
        assert row.hash_key == _HASH_KEY and row.hash_iv == _HASH_IV
        assert _HASH_KEY.encode() not in row.hash_key_enc
        assert _HASH_IV.encode() not in row.hash_iv_enc
        assert row.updated_by_user_id == db.query(User).filter_by(email=email).one().id
        issuer = get_invoice_issuer(db)
        assert isinstance(issuer, EcpayInvoiceIssuer)
        assert issuer._merchant_id == "2000132"
        assert db.query(AuditLog).filter_by(
            action="platform.invoice.ecpay.update"
        ).count() == 1

    page = client.get("/ui/admin/invoice-settings")
    assert page.status_code == 200
    assert "綠界發票已啟用" in page.text
    assert "資料庫加密設定" in page.text
    assert _HASH_KEY not in page.text and _HASH_IV not in page.text
    assert "不需修改 .env 或重啟" in page.text
    assert "與綠界金流憑證是不同的一組" in page.text


def test_blank_secrets_preserve_existing_values(client):
    _login(client, admin=True)
    with _Session() as db:
        _save(db)
        db.commit()
    response = client.post(
        "/ui/admin/invoice-settings/ecpay",
        data={
            "merchant_id": "3000007",
            "hash_key": "",
            "hash_iv": "",
            "environment": "stage",
        },
    )
    assert response.status_code == 303
    with _Session() as db:
        row = db.query(PlatformInvoiceConfig).one()
        assert row.merchant_id == "3000007"
        assert row.hash_key == _HASH_KEY and row.hash_iv == _HASH_IV


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("hash_key", "too-short", "HashKey 必須恰好為 16 bytes"),
        ("hash_iv", "too-short", "HashIV 必須恰好為 16 bytes"),
    ],
)
def test_rejects_invalid_aes_secret_lengths(client, field, value, message):
    _login(client, admin=True)
    data = {
        "merchant_id": "2000132",
        "hash_key": _HASH_KEY,
        "hash_iv": _HASH_IV,
        "environment": "stage",
    }
    data[field] = value
    response = client.post("/ui/admin/invoice-settings/ecpay", data=data)
    assert response.status_code == 400
    assert message in response.text


def test_production_rejects_public_test_merchant(client):
    _login(client, admin=True)
    response = client.post(
        "/ui/admin/invoice-settings/ecpay",
        data={
            "merchant_id": "2000132",
            "hash_key": _HASH_KEY,
            "hash_iv": _HASH_IV,
            "environment": "prod",
        },
    )
    assert response.status_code == 400
    assert "正式環境不可使用" in response.text


def test_self_check_uses_database_credentials(client):
    _login(client, admin=True)
    with _Session() as db:
        _save(db)
        db.commit()
    response = client.post("/ui/admin/invoice-settings/check")
    assert response.status_code == 200
    assert "AES-128-CBC 加解密自我檢查均通過" in response.text
    with _Session() as db:
        assert db.query(AuditLog).filter_by(
            action="platform.invoice.ecpay.check"
        ).count() == 1


def test_failed_invoice_blocks_rotation_disable_and_reset(client):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        _save(db, actor_id=user.id)
        db.add(Invoice(
            tenant_id=user.tenant_id,
            relate_number="SCFAILED001",
            amount_cents=89900,
            status="failed",
            provider="ecpay",
            error_msg="temporary",
        ))
        db.commit()

    rotate = client.post(
        "/ui/admin/invoice-settings/ecpay",
        data={
            "merchant_id": "3000007",
            "hash_key": _HASH_KEY,
            "hash_iv": _HASH_IV,
            "environment": "stage",
        },
    )
    assert rotate.status_code == 400
    assert "仍有等待開立或開立失敗" in rotate.text
    assert client.post("/ui/admin/invoice-settings/disable").status_code == 400
    assert client.post("/ui/admin/invoice-settings/reset").status_code == 400


def test_retry_button_reissues_failed_stub_invoice(client):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        row = Invoice(
            tenant_id=user.tenant_id,
            relate_number="SCRETRY001",
            amount_cents=89900,
            buyer_email=email,
            status="failed",
            provider="stub",
            error_msg="temporary",
        )
        db.add(row)
        db.commit()
    response = client.post("/ui/admin/invoice-settings/retry")
    assert response.status_code == 303
    assert response.headers["location"].endswith("?retried=1")
    with _Session() as db:
        row = db.query(Invoice).filter_by(relate_number="SCRETRY001").one()
        assert row.status == "issued" and row.error_msg is None


def test_admin_can_void_issued_invoice_and_audit(client):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        row = Invoice(
            tenant_id=user.tenant_id,
            relate_number="SCVOIDUI001",
            invoice_no="ST12345678",
            invoice_date="2030-06-15",
            amount_cents=89900,
            buyer_email=email,
            status="issued",
            provider="stub",
        )
        db.add(row)
        db.commit()
        invoice_id = row.id
    response = client.post(
        f"/ui/admin/invoice-settings/{invoice_id}/void",
        data={"reason": "訂單退款"},
    )
    assert response.status_code == 303
    assert "voided=ST12345678" in response.headers["location"]
    with _Session() as db:
        row = db.get(Invoice, invoice_id)
        assert row.status == "void" and row.void_reason == "訂單退款"
        assert db.query(AuditLog).filter_by(
            action="platform.invoice.void",
            target=f"invoice:{invoice_id}",
        ).count() == 1
    page = client.get("/ui/admin/invoice-settings")
    assert "已作廢" in page.text
    assert "訂單退款" in page.text


def test_void_provider_failure_is_visible_and_audited(client, monkeypatch):
    from saas_mvp.services import invoices as invoices_svc
    from saas_mvp.services.invoice_ecpay import InvoiceError, StubInvoiceIssuer

    class Boom(StubInvoiceIssuer):
        def void(self, **kwargs):
            raise InvoiceError("invoice already allowed")

    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        row = Invoice(
            tenant_id=user.tenant_id,
            relate_number="SCVOIDFAIL1",
            invoice_no="ST87654321",
            invoice_date="2030-06-15",
            amount_cents=89900,
            status="issued",
            provider="stub",
        )
        db.add(row)
        db.commit()
        invoice_id = row.id
    monkeypatch.setattr(invoices_svc, "get_invoice_issuer", lambda *a, **k: Boom())
    response = client.post(
        f"/ui/admin/invoice-settings/{invoice_id}/void",
        data={"reason": "退款"},
    )
    assert response.status_code == 502
    assert "綠界拒絕作廢" in response.text
    assert "invoice already allowed" in response.text
    with _Session() as db:
        row = db.get(Invoice, invoice_id)
        assert row.status == "issued"
        assert "invoice already allowed" in row.void_error_msg
        assert db.query(AuditLog).filter_by(
            action="platform.invoice.void_failed"
        ).count() == 1


def test_open_ecpay_invoice_blocks_merchant_change_and_reset_but_allows_key_rotation(
    client,
):
    email = _login(client, admin=True)
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        _save(db, actor_id=user.id)
        db.add(Invoice(
            tenant_id=user.tenant_id,
            relate_number="SCECPAYOPEN1",
            invoice_no="AB12345678",
            invoice_date="2030-06-15",
            amount_cents=89900,
            status="issued",
            provider="ecpay",
        ))
        db.commit()

    changed_merchant = client.post(
        "/ui/admin/invoice-settings/ecpay",
        data={
            "merchant_id": "3000007",
            "hash_key": "",
            "hash_iv": "",
            "environment": "stage",
        },
    )
    assert changed_merchant.status_code == 400
    assert "尚未作廢的綠界發票" in changed_merchant.text
    assert client.post("/ui/admin/invoice-settings/reset").status_code == 400

    rotate_keys = client.post(
        "/ui/admin/invoice-settings/ecpay",
        data={
            "merchant_id": "2000132",
            "hash_key": "1234567890abcdef",
            "hash_iv": "fedcba0987654321",
            "environment": "stage",
        },
    )
    assert rotate_keys.status_code == 303


def test_disable_and_reset_use_database_override(client):
    _login(client, admin=True)
    with _Session() as db:
        _save(db)
        db.commit()
    assert client.post("/ui/admin/invoice-settings/disable").status_code == 303
    with _Session() as db:
        assert service.effective_invoice_config(db, settings).provider == "stub"
    assert client.post("/ui/admin/invoice-settings/reset").status_code == 303
    with _Session() as db:
        assert db.query(PlatformInvoiceConfig).count() == 0


def test_disabling_environment_ecpay_snapshots_credentials_for_later_void(
    client, monkeypatch
):
    email = _login(client, admin=True)
    monkeypatch.setattr(settings, "invoice_provider", "ecpay")
    monkeypatch.setattr(settings, "ecpay_invoice_merchant_id", "2000132")
    monkeypatch.setattr(settings, "ecpay_invoice_hash_key", _HASH_KEY)
    monkeypatch.setattr(settings, "ecpay_invoice_hash_iv", _HASH_IV)
    monkeypatch.setattr(settings, "ecpay_invoice_env", "stage")
    with _Session() as db:
        user = db.query(User).filter_by(email=email).one()
        db.add(Invoice(
            tenant_id=user.tenant_id,
            relate_number="SCENVOPEN001",
            invoice_no="AB87654321",
            invoice_date="2030-06-15",
            amount_cents=89900,
            status="issued",
            provider="ecpay",
        ))
        db.commit()
    assert client.post("/ui/admin/invoice-settings/disable").status_code == 303
    with _Session() as db:
        row = db.query(PlatformInvoiceConfig).one()
        assert row.provider == "stub"
        assert row.merchant_id == "2000132"
        assert row.hash_key == _HASH_KEY and row.hash_iv == _HASH_IV
        issuer = get_invoice_issuer(db, provider="ecpay")
        assert isinstance(issuer, EcpayInvoiceIssuer)
        assert issuer._merchant_id == "2000132"


def test_readiness_recognizes_database_invoice_config(client):
    _login(client, admin=True)
    with _Session() as db:
        _save(db)
        db.commit()
    checks = {item.name: item for item in run_checks(session_factory=_Session)}
    assert checks["invoice"].status == "PASS"
    assert "source=database" in checks["invoice"].detail

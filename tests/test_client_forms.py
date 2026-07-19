"""預約諮詢表／同意書：派發、快照、公開填寫、稽核與租戶隔離。"""

from __future__ import annotations

import datetime
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.config import settings
from saas_mvp.db import Base, get_db
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.client_form import ClientFormRequest
from saas_mvp.models.customer import Customer
from saas_mvp.models.service import Service
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.routers.line_webhook import _confirm_text
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import client_forms as forms_svc

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
    with _Session() as session:
        yield session


@pytest.fixture()
def client(db):
    app = create_app()

    def override_db():
        with _Session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def _seed(db, suffix: str = "a"):
    tenant = Tenant(name=f"forms-{suffix}-{uuid.uuid4().hex[:6]}", plan="pro")
    db.add(tenant)
    db.flush()
    customer = Customer(
        tenant_id=tenant.id,
        line_user_id=f"U-form-{suffix}-{uuid.uuid4().hex[:6]}",
        display_name=f"顧客 {suffix}",
    )
    service = Service(
        tenant_id=tenant.id,
        name=f"療程 {suffix}",
        duration_minutes=60,
        price_cents=120000,
    )
    slot = BookingSlot(
        tenant_id=tenant.id,
        slot_start=datetime.datetime(2032, 5, 1, 10, tzinfo=datetime.timezone.utc),
        max_capacity=2,
    )
    db.add_all([customer, service, slot])
    db.commit()
    return tenant, customer, service, slot


def _active_template(db, tenant, service):
    template = forms_svc.create_template(
        db,
        tenant_id=tenant.id,
        name="療程健康諮詢與同意書",
        intro="請於預約前如實填寫。",
        consent_text="本人確認上述資料正確，並已了解療程內容與相關注意事項。",
        service_id=service.id,
        require_signature=True,
    )
    forms_svc.add_question(
        db,
        tenant_id=tenant.id,
        template_id=template.id,
        label="姓名",
        field_type="text",
        required=True,
    )
    forms_svc.add_question(
        db,
        tenant_id=tenant.id,
        template_id=template.id,
        label="過敏史",
        field_type="select",
        required=True,
        options="無\n藥物\n食物",
    )
    forms_svc.add_question(
        db,
        tenant_id=tenant.id,
        template_id=template.id,
        label="最近就醫日期",
        field_type="date",
        required=False,
    )
    forms_svc.add_question(
        db,
        tenant_id=tenant.id,
        template_id=template.id,
        label="資料均為本人填寫",
        field_type="checkbox",
        required=True,
    )
    forms_svc.set_active(db, tenant_id=tenant.id, template_id=template.id, active=True)
    db.commit()
    return template


def _book(db, tenant, customer, service, slot):
    return booking_svc.book_slot(
        db,
        tenant_id=tenant.id,
        slot_id=slot.id,
        line_user_id=customer.line_user_id,
        service_id=service.id,
    )


def _login_owner(client: TestClient) -> tuple[int, str]:
    email = f"forms-owner-{uuid.uuid4().hex[:8]}@example.com"
    password = "Test1234!"
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": password,
            "tenant_name": f"forms-ui-{uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    with _Session() as session:
        user = session.query(User).filter_by(email=email).one()
        tenant_id = user.tenant_id
        session.get(Tenant, tenant_id).plan = "pro"
        session.commit()
    assert (
        client.post(
            "/ui/login", data={"email": email, "password": password}
        ).status_code
        == 303
    )
    return tenant_id, email


def test_booking_attaches_immutable_template_snapshot_and_line_link(db, monkeypatch):
    tenant, customer, service, slot = _seed(db)
    template = _active_template(db, tenant, service)
    reservation = _book(db, tenant, customer, service, slot)

    requests = forms_svc.for_reservation(
        db, tenant_id=tenant.id, reservation_id=reservation.id
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.customer_id == customer.id
    assert request.template_version == template.version
    original_snapshot = json.loads(request.questions_json)
    assert [question["label"] for question in original_snapshot] == [
        "姓名",
        "過敏史",
        "最近就醫日期",
        "資料均為本人填寫",
    ]

    forms_svc.add_question(
        db,
        tenant_id=tenant.id,
        template_id=template.id,
        label="範本後來新增的問題",
        field_type="text",
        required=False,
    )
    db.commit()
    db.refresh(request)
    assert json.loads(request.questions_json) == original_snapshot

    monkeypatch.setattr(settings, "public_base_url", "https://forms.example.test")
    message = _confirm_text(db, tenant.id, reservation, slot.id)
    assert "預約前請完成" in message
    assert f"https://forms.example.test/client-forms/{request.token}" in message


def test_public_form_validates_then_becomes_read_only(client, db):
    tenant, customer, service, slot = _seed(db)
    _active_template(db, tenant, service)
    reservation = _book(db, tenant, customer, service, slot)
    request = db.query(ClientFormRequest).filter_by(reservation_id=reservation.id).one()
    questions = json.loads(request.questions_json)
    by_label = {question["label"]: str(question["id"]) for question in questions}

    page = client.get(f"/client-forms/{request.token}")
    assert page.status_code == 200
    assert page.headers["cache-control"].startswith("no-store")
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "療程健康諮詢與同意書" in page.text

    invalid = client.post(
        f"/client-forms/{request.token}",
        data={"consent": "true", "signer_name": "王小明"},
    )
    assert invalid.status_code == 422
    assert "請填寫：姓名" in invalid.text

    valid = client.post(
        f"/client-forms/{request.token}",
        data={
            f"q_{by_label['姓名']}": "王小明",
            f"q_{by_label['過敏史']}": "無",
            f"q_{by_label['最近就醫日期']}": "2032-04-01",
            f"q_{by_label['資料均為本人填寫']}": "true",
            "consent": "true",
            "signer_name": "王小明",
        },
        headers={"user-agent": "client-form-test"},
    )
    assert valid.status_code == 200
    assert "已安全保存" in valid.text
    assert "列印／另存 PDF" in valid.text

    with _Session() as check:
        saved = check.query(ClientFormRequest).filter_by(id=request.id).one()
        assert saved.status == "completed"
        assert saved.signer_name == "王小明"
        assert saved.signed_at is not None and saved.completed_at is not None
        assert saved.submitted_user_agent == "client-form-test"
        answers = json.loads(saved.answers_json)
        assert answers[by_label["過敏史"]] == "無"
        assert answers[by_label["資料均為本人填寫"]] is True

    replay = client.post(
        f"/client-forms/{request.token}",
        data={
            f"q_{by_label['姓名']}": "惡意覆寫",
            "consent": "true",
            "signer_name": "其他人",
        },
    )
    assert replay.status_code == 200
    assert "王小明" in replay.text
    assert "惡意覆寫" not in replay.text


def test_cancelled_reservation_stops_form_submission(client, db):
    tenant, customer, service, slot = _seed(db)
    _active_template(db, tenant, service)
    reservation = _book(db, tenant, customer, service, slot)
    request = db.query(ClientFormRequest).filter_by(reservation_id=reservation.id).one()
    booking_svc.cancel_reservation(
        db, tenant_id=tenant.id, reservation_id=reservation.id
    )

    page = client.get(f"/client-forms/{request.token}")
    assert page.status_code == 200
    assert "預約已取消" in page.text
    assert "確認並送出" not in page.text

    response = client.post(
        f"/client-forms/{request.token}",
        data={"consent": "true", "signer_name": "王小明"},
    )
    assert response.status_code == 422
    assert "預約已取消" in response.text


def test_service_binding_and_lookups_are_tenant_isolated(db):
    tenant_a, customer_a, service_a, slot_a = _seed(db, "a")
    tenant_b, _, service_b, _ = _seed(db, "b")
    with pytest.raises(forms_svc.ClientFormError, match="綁定的服務不存在"):
        forms_svc.create_template(
            db,
            tenant_id=tenant_a.id,
            name="跨租戶表單",
            intro="",
            consent_text="這是一段足夠長的同意聲明文字。",
            service_id=service_b.id,
        )

    template = _active_template(db, tenant_a, service_a)
    reservation = _book(db, tenant_a, customer_a, service_a, slot_a)
    request = db.query(ClientFormRequest).filter_by(template_id=template.id).one()
    assert (
        forms_svc.for_reservation(
            db, tenant_id=tenant_b.id, reservation_id=reservation.id
        )
        == []
    )
    assert (
        forms_svc.for_customer(db, tenant_id=tenant_b.id, customer_id=customer_a.id)
        == []
    )
    assert request.tenant_id == tenant_a.id


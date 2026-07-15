"""Versioned API organization/RBAC contract tests."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, get_db
from saas_mvp.models.organization import OrganizationMember, TenantMember
from saas_mvp.models.user import User


@pytest.fixture()
def v1_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    app = create_app()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, session_factory


def _register(client: TestClient, prefix: str = "org") -> tuple[dict, dict[str, str]]:
    unique = uuid.uuid4().hex[:8]
    response = client.post(
        "/auth/register",
        json={
            "email": f"{prefix}-{unique}@example.com",
            "password": "safe-password-123",
            "tenant_name": f"{prefix}-{unique}",
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    return response.json(), {"Authorization": f"Bearer {token}"}


def test_registration_creates_owner_context(v1_client):
    client, _ = v1_client
    _, headers = _register(client)

    response = client.get("/api/v1/context", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["organization"]["role"] == "owner"
    assert body["tenant"]["role"] == "owner"
    assert body["organization"]["share_customers"] is False
    assert "organization:manage" in body["permissions"]
    assert "operations:write" in body["permissions"]


def test_owner_can_update_explicit_sharing_policy(v1_client):
    client, _ = v1_client
    _, headers = _register(client, "sharing")

    response = client.patch(
        "/api/v1/organization",
        headers=headers,
        json={"share_customers": True, "share_loyalty": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["share_customers"] is True
    assert response.json()["share_loyalty"] is True
    assert response.json()["share_coupons"] is False


def test_non_owner_cannot_manage_organization(v1_client):
    client, session_factory = v1_client
    _, headers = _register(client, "viewer")
    with session_factory() as db:
        user = db.query(User).filter(User.email.like("viewer-%")).one()
        db.query(OrganizationMember).filter_by(user_id=user.id).update(
            {"role": "viewer"}
        )
        db.query(TenantMember).filter_by(user_id=user.id).update({"role": "viewer"})
        db.commit()

    response = client.patch(
        "/api/v1/organization", headers=headers, json={"share_customers": True}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: organization:manage"


def test_owner_member_list_is_organization_scoped(v1_client):
    client, _ = v1_client
    _, first_headers = _register(client, "first")
    _register(client, "second")

    response = client.get("/api/v1/organization/members", headers=first_headers)

    assert response.status_code == 200, response.text
    members = response.json()
    assert len(members) == 1
    assert members[0]["email"].startswith("first-")
    assert members[0]["role"] == "owner"

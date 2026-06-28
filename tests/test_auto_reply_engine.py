import base64
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.db import Base, import_all_models
from saas_mvp.db import get_db
from saas_mvp.line_client import FakeLineReplyClient, get_line_client
from saas_mvp.models.auto_reply_rule import AutoReplyRule
from saas_mvp.models.line_message import DIRECTION_IN, DIRECTION_OUT, LineMessage
from saas_mvp.models.line_channel_config import VALID_BOT_MODES, validate_bot_mode
from saas_mvp.services import auto_reply as auto_reply_svc
from saas_mvp.models.tenant import Tenant


_CHANNEL_SECRET = "auto-reply-test-secret"
_ACCESS_TOKEN = "auto-reply-test-token"


def _rule(
    *,
    id: int,
    keyword: str,
    match_type: str = "contains",
    priority: int = 0,
    is_active: bool = True,
):
    rule = AutoReplyRule(keyword=keyword)
    rule.id = id
    rule.match_type = match_type
    rule.priority = priority
    rule.is_active = is_active
    return rule


def test_auto_reply_mode_is_valid_bot_mode():
    assert "auto_reply" in VALID_BOT_MODES
    assert validate_bot_mode("auto_reply") == "auto_reply"


def test_auto_reply_rule_table_columns_and_defaults():
    import_all_models()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    assert "auto_reply_rules" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("auto_reply_rules")}
    assert {
        "id",
        "tenant_id",
        "keyword",
        "match_type",
        "reply_type",
        "reply_text",
        "flex_menu_id",
        "priority",
        "is_active",
        "created_at",
        "updated_at",
    } <= columns

    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    try:
        tenant = Tenant(name="auto-reply-rule-test")
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        rule = AutoReplyRule(tenant_id=tenant.id, keyword="hello")
        db.add(rule)
        db.commit()
        db.refresh(rule)

        assert rule.match_type == "contains"
        assert rule.reply_type == "text"
        assert rule.priority == 0
        assert rule.is_active is True
        assert rule.created_at is not None
        assert rule.updated_at is not None
    finally:
        db.close()


_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def client():
    import_all_models()
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()
    fake_line_client = FakeLineReplyClient()

    def override_get_db():
        db = _Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_line_client] = lambda: fake_line_client
    app.state.fake_line_client = fake_line_client
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _register(client: TestClient) -> str:
    resp = client.post(
        "/auth/register",
        json={
            "email": f"auto_{uuid.uuid4().hex[:8]}@example.com",
            "password": "Test1234!",
            "tenant_name": f"auto_{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant_id(client: TestClient, token: str) -> int:
    resp = client.get("/tenants/me", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _configure_auto_reply_line(tenant_id: int) -> None:
    from saas_mvp.services.line_config import upsert_line_config

    db = _Session()
    try:
        upsert_line_config(
            db,
            tenant_id,
            channel_secret=_CHANNEL_SECRET,
            access_token=_ACCESS_TOKEN,
            default_target_lang="zh-TW",
            bot_mode="auto_reply",
            bot_info_client=None,
        )
    finally:
        db.close()


def _sign(body: bytes) -> str:
    mac = hmac.new(_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(mac).decode("utf-8")


def _text_payload(text: str, *, reply_token: str = "rt-auto") -> bytes:
    return json.dumps(
        {
            "events": [
                {
                    "type": "message",
                    "replyToken": reply_token,
                    "source": {"type": "user", "userId": "U-auto-001"},
                    "message": {"type": "text", "text": text},
                }
            ]
        }
    ).encode("utf-8")


def _post_line_webhook(client: TestClient, tenant_id: int, body: bytes):
    return client.post(
        f"/line/webhook/{tenant_id}",
        content=body,
        headers={"X-Line-Signature": _sign(body), "Content-Type": "application/json"},
    )


def test_auto_reply_rule_api_crud(client):
    token = _register(client)
    headers = _auth(token)

    created = client.post(
        "/api/auto-reply-rules/",
        headers=headers,
        json={
            "keyword": "  hello  ",
            "match_type": "contains",
            "reply_type": "text",
            "reply_text": "Hi there",
            "priority": 5,
        },
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["keyword"] == "hello"
    assert rule["reply_text"] == "Hi there"
    assert rule["flex_menu_id"] is None

    listed = client.get("/api/auto-reply-rules/", headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [rule["id"]]

    updated = client.put(
        f"/api/auto-reply-rules/{rule['id']}",
        headers=headers,
        json={
            "keyword": "bye",
            "match_type": "exact",
            "reply_text": "Goodbye",
            "priority": 1,
            "is_active": False,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["keyword"] == "bye"
    assert body["match_type"] == "exact"
    assert body["reply_text"] == "Goodbye"
    assert body["priority"] == 1
    assert body["is_active"] is False

    active = client.get("/api/auto-reply-rules/?active_only=true", headers=headers)
    assert active.status_code == 200
    assert active.json() == []

    deleted = client.delete(f"/api/auto-reply-rules/{rule['id']}", headers=headers)
    assert deleted.status_code == 204
    assert client.get("/api/auto-reply-rules/", headers=headers).json() == []


def test_auto_reply_rule_api_tenant_isolation(client):
    token_a = _register(client)
    token_b = _register(client)

    created = client.post(
        "/api/auto-reply-rules/",
        headers=_auth(token_a),
        json={"keyword": "hello", "reply_type": "text", "reply_text": "A"},
    )
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]

    assert client.get("/api/auto-reply-rules/", headers=_auth(token_b)).json() == []
    assert (
        client.get(f"/api/auto-reply-rules/{rule_id}", headers=_auth(token_b)).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/auto-reply-rules/{rule_id}",
            headers=_auth(token_b),
            json={"reply_text": "stolen"},
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/auto-reply-rules/{rule_id}", headers=_auth(token_b)).status_code
        == 404
    )


def test_webhook_auto_reply_text_rule_replies_and_records_messages(client):
    token = _register(client)
    tenant_id = _tenant_id(client, token)
    _configure_auto_reply_line(tenant_id)

    created = client.post(
        "/api/auto-reply-rules/",
        headers=_auth(token),
        json={
            "keyword": "hello",
            "match_type": "contains",
            "reply_type": "text",
            "reply_text": "Hi from auto reply",
        },
    )
    assert created.status_code == 201, created.text

    body = _text_payload("well hello there", reply_token="rt-auto-text")
    resp = _post_line_webhook(client, tenant_id, body)

    assert resp.status_code == 200, resp.text
    fake = client.app.state.fake_line_client
    assert fake.sent[-1].reply_token == "rt-auto-text"
    assert fake.sent[-1].text == "Hi from auto reply"
    assert fake.flex == []

    db = _Session()
    try:
        rows = (
            db.query(LineMessage)
            .filter(
                LineMessage.tenant_id == tenant_id,
                LineMessage.line_user_id == "U-auto-001",
            )
            .order_by(LineMessage.id)
            .all()
        )
        assert [(row.direction, row.text) for row in rows] == [
            (DIRECTION_IN, "well hello there"),
            (DIRECTION_OUT, "Hi from auto reply"),
        ]
    finally:
        db.close()


def test_webhook_auto_reply_flex_rule_uses_reply_flex(client):
    from saas_mvp.services import flex_menu as flex_svc

    token = _register(client)
    tenant_id = _tenant_id(client, token)
    _configure_auto_reply_line(tenant_id)

    db = _Session()
    try:
        menu = flex_svc.create_menu(db, tenant_id=tenant_id, title="店內選單")
        flex_svc.add_card(
            db,
            tenant_id=tenant_id,
            menu_id=menu.id,
            title="預約",
            action_type="message",
            action_data="預約",
        )
        menu_id = menu.id
    finally:
        db.close()

    created = client.post(
        "/api/auto-reply-rules/",
        headers=_auth(token),
        json={
            "keyword": "menu",
            "match_type": "exact",
            "reply_type": "flex",
            "flex_menu_id": menu_id,
        },
    )
    assert created.status_code == 201, created.text

    body = _text_payload("menu", reply_token="rt-auto-flex")
    resp = _post_line_webhook(client, tenant_id, body)

    assert resp.status_code == 200, resp.text
    fake = client.app.state.fake_line_client
    assert fake.sent == []
    assert fake.flex[-1].reply_token == "rt-auto-flex"
    assert fake.flex[-1].alt_text == "店內選單"
    assert fake.flex[-1].contents["type"] == "carousel"

    db = _Session()
    try:
        rows = (
            db.query(LineMessage)
            .filter(
                LineMessage.tenant_id == tenant_id,
                LineMessage.line_user_id == "U-auto-001",
            )
            .order_by(LineMessage.id)
            .all()
        )
        assert [(row.direction, row.text) for row in rows] == [
            (DIRECTION_IN, "menu"),
            (DIRECTION_OUT, "店內選單"),
        ]
    finally:
        db.close()


def test_webhook_auto_reply_no_match_does_not_reply(client):
    token = _register(client)
    tenant_id = _tenant_id(client, token)
    _configure_auto_reply_line(tenant_id)

    created = client.post(
        "/api/auto-reply-rules/",
        headers=_auth(token),
        json={
            "keyword": "hello",
            "match_type": "exact",
            "reply_type": "text",
            "reply_text": "Hi",
        },
    )
    assert created.status_code == 201, created.text

    body = _text_payload("not matched", reply_token="rt-auto-miss")
    resp = _post_line_webhook(client, tenant_id, body)

    assert resp.status_code == 200, resp.text
    fake = client.app.state.fake_line_client
    assert fake.sent == []
    assert fake.flex == []

    db = _Session()
    try:
        rows = (
            db.query(LineMessage)
            .filter(
                LineMessage.tenant_id == tenant_id,
                LineMessage.line_user_id == "U-auto-001",
            )
            .order_by(LineMessage.id)
            .all()
        )
        assert [(row.direction, row.text) for row in rows] == [
            (DIRECTION_IN, "not matched"),
        ]
    finally:
        db.close()


def test_auto_reply_rule_flex_menu_must_belong_to_current_tenant(client):
    token_a = _register(client)
    token_b = _register(client)

    menu = client.post(
        "/booking/flex-menu/",
        headers=_auth(token_a),
        json={"title": "A menu"},
    )
    assert menu.status_code == 201, menu.text
    menu_id = menu.json()["id"]

    ok = client.post(
        "/api/auto-reply-rules/",
        headers=_auth(token_a),
        json={
            "keyword": "menu",
            "reply_type": "flex",
            "flex_menu_id": menu_id,
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["reply_text"] is None
    assert ok.json()["flex_menu_id"] == menu_id

    leaked = client.post(
        "/api/auto-reply-rules/",
        headers=_auth(token_b),
        json={
            "keyword": "menu",
            "reply_type": "flex",
            "flex_menu_id": menu_id,
        },
    )
    assert leaked.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"keyword": "   ", "reply_type": "text", "reply_text": "x"},
        {"keyword": "hello", "reply_type": "text"},
        {"keyword": "hello", "reply_type": "flex"},
        {"keyword": "hello", "match_type": "bad", "reply_type": "text", "reply_text": "x"},
        {"keyword": "hello", "reply_type": "bad", "reply_text": "x"},
    ],
)
def test_auto_reply_rule_api_rejects_invalid_invariants(client, payload):
    token = _register(client)
    resp = client.post(
        "/api/auto-reply-rules/",
        headers=_auth(token),
        json=payload,
    )
    assert resp.status_code == 422


def test_auto_reply_rule_api_rejects_null_required_update_fields(client):
    token = _register(client)
    headers = _auth(token)
    rule = client.post(
        "/api/auto-reply-rules/",
        headers=headers,
        json={"keyword": "hello", "reply_type": "text", "reply_text": "Hi"},
    ).json()

    clear_text = client.put(
        f"/api/auto-reply-rules/{rule['id']}",
        headers=headers,
        json={"reply_text": None},
    )
    assert clear_text.status_code == 422

    menu = client.post(
        "/booking/flex-menu/",
        headers=headers,
        json={"title": "menu"},
    ).json()
    flex_rule = client.post(
        "/api/auto-reply-rules/",
        headers=headers,
        json={
            "keyword": "menu",
            "reply_type": "flex",
            "flex_menu_id": menu["id"],
        },
    ).json()

    clear_menu = client.put(
        f"/api/auto-reply-rules/{flex_rule['id']}",
        headers=headers,
        json={"flex_menu_id": None},
    )
    assert clear_menu.status_code == 422


def test_auto_reply_match_returns_none_without_match():
    rules = [
        _rule(id=1, keyword="hello", match_type="exact"),
        _rule(id=2, keyword="vip", match_type="contains", is_active=False),
    ]

    assert auto_reply_svc.match(rules, "no match") is None


@pytest.mark.parametrize(
    ("match_type", "keyword", "text"),
    [
        ("exact", "hello", "hello"),
        ("prefix", "HEL", "hello there"),
        ("contains", "VIP", "hello vip customer"),
    ],
)
def test_auto_reply_match_supports_each_match_type(match_type, keyword, text):
    rule = _rule(id=1, keyword=keyword, match_type=match_type)

    assert auto_reply_svc.match([rule], text) is rule


def test_auto_reply_match_type_order_exact_prefix_contains():
    exact = _rule(id=3, keyword="hello", match_type="exact", priority=99)
    prefix = _rule(id=2, keyword="hell", match_type="prefix", priority=-10)
    contains = _rule(id=1, keyword="ell", match_type="contains", priority=-20)

    assert auto_reply_svc.match([contains, prefix, exact], "hello") is exact


def test_auto_reply_exact_is_case_sensitive_but_contains_is_not():
    exact = _rule(id=1, keyword="hello", match_type="exact")
    contains = _rule(id=2, keyword="HELLO", match_type="contains")

    assert auto_reply_svc.match([exact, contains], "Hello") is contains


def test_auto_reply_match_priority_and_id_tie_break_same_type():
    high_priority = _rule(id=1, keyword="vip", match_type="contains", priority=10)
    low_priority = _rule(id=9, keyword="vip", match_type="contains", priority=1)
    same_priority_lower_id = _rule(
        id=5, keyword="vip", match_type="contains", priority=1
    )

    assert (
        auto_reply_svc.match(
            [high_priority, low_priority, same_priority_lower_id],
            "VIP customer",
        )
        is same_priority_lower_id
    )

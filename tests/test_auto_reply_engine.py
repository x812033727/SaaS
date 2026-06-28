from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base, import_all_models
from saas_mvp.models.auto_reply_rule import AutoReplyRule
from saas_mvp.models.line_channel_config import VALID_BOT_MODES, validate_bot_mode
from saas_mvp.services import auto_reply as auto_reply_svc
from saas_mvp.models.tenant import Tenant


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


def test_auto_reply_match_returns_none_without_match():
    rules = [
        _rule(id=1, keyword="hello", match_type="exact"),
        _rule(id=2, keyword="vip", match_type="contains", is_active=False),
    ]

    assert auto_reply_svc.match(rules, "no match") is None


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

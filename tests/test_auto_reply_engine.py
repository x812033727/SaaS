from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base, import_all_models
from saas_mvp.models.auto_reply_rule import AutoReplyRule
from saas_mvp.models.line_channel_config import VALID_BOT_MODES, validate_bot_mode
from saas_mvp.models.tenant import Tenant


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

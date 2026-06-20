from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base
from saas_mvp.models.line_webhook_event import LineWebhookEvent
from saas_mvp.models.tenant import Tenant


def test_line_webhook_event_table_columns_and_unique_constraint():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    assert "line_webhook_events" in inspector.get_table_names()

    columns = {
        column["name"] for column in inspector.get_columns("line_webhook_events")
    }
    assert {
        "id",
        "tenant_id",
        "webhook_event_id",
        "status",
        "attempt_count",
        "last_error",
        "last_stage",
        "created_at",
        "updated_at",
        "processed_at",
    } <= columns

    unique_columns = [
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("line_webhook_events")
    ]
    assert ("tenant_id", "webhook_event_id") in unique_columns

    index_names = {
        index["name"] for index in inspector.get_indexes("line_webhook_events")
    }
    assert "ix_line_webhook_events_created_status" not in index_names


def test_line_webhook_event_defaults_and_duplicate_guard():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = Session()
    try:
        tenant = Tenant(name="line-webhook-event-test")
        db.add(tenant)
        db.commit()
        db.refresh(tenant)

        row = LineWebhookEvent(tenant_id=tenant.id, webhook_event_id="evt-1")
        db.add(row)
        db.commit()
        db.refresh(row)

        assert row.status == "pending"
        assert row.attempt_count == 0

        db.add(LineWebhookEvent(tenant_id=tenant.id, webhook_event_id="evt-1"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        else:  # pragma: no cover - commit must fail
            raise AssertionError("duplicate webhook event id was accepted")
    finally:
        db.close()

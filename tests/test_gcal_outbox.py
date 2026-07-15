"""Google Calendar reliable outbox：重試、最新意圖與冪等事件。"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.gcal_sync_job import (
    GCAL_ACTION_DELETE,
    GCAL_SYNC_FAILED,
    GCAL_SYNC_PENDING,
    GCAL_SYNC_SYNCED,
    GcalSyncJob,
)
from saas_mvp.models.reservation import (
    RESERVATION_CANCELLED,
    RESERVATION_CONFIRMED,
    Reservation,
)
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential
from saas_mvp.ops.retry_gcal_syncs import retry_gcal_syncs
from saas_mvp.services import gcal as gcal_svc
from saas_mvp.services.gcal import StubGcalClient

_NOW = datetime.datetime(2030, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _reservation(factory) -> int:
    with factory() as db:
        tenant = Tenant(name="gcal-outbox", plan="pro")
        db.add(tenant)
        db.flush()
        cred = TenantGcalCredential(tenant_id=tenant.id, calendar_id="primary")
        cred.refresh_token = "encrypted-refresh-token"
        db.add(cred)
        slot = BookingSlot(
            tenant_id=tenant.id,
            slot_start=_NOW + datetime.timedelta(days=1),
            max_capacity=3,
            booked_count=1,
        )
        db.add(slot)
        db.flush()
        reservation = Reservation(
            tenant_id=tenant.id,
            slot_id=slot.id,
            party_size=1,
            status=RESERVATION_CONFIRMED,
        )
        db.add(reservation)
        db.flush()
        gcal_svc.enqueue_reservation_sync(db, reservation, "create", now=_NOW)
        db.commit()
        return reservation.id


class _Unavailable(StubGcalClient):
    def insert_event(self, **kwargs):
        raise gcal_svc.GcalError("temporary Google outage")

    def patch_event(self, **kwargs):
        raise gcal_svc.GcalError("temporary Google outage")

    def delete_event(self, **kwargs):
        raise gcal_svc.GcalError("temporary Google outage")


def test_failure_is_persisted_and_scheduler_retries(factory):
    reservation_id = _reservation(factory)
    with factory() as db:
        row = db.execute(select(GcalSyncJob)).scalar_one()
        result = gcal_svc.attempt_sync(db, row, client=_Unavailable(), now=_NOW)
        db.commit()
        assert result == GCAL_SYNC_PENDING
        assert row.attempt_count == 1
        assert "temporary Google outage" in row.last_error

    stub = StubGcalClient()
    results = retry_gcal_syncs(
        apply=True,
        now=_NOW + datetime.timedelta(minutes=2),
        session_factory=factory,
        client=stub,
    )
    assert results and results[0][1] == GCAL_SYNC_SYNCED
    with factory() as db:
        row = db.execute(select(GcalSyncJob)).scalar_one()
        reservation = db.get(Reservation, reservation_id)
        assert row.status == GCAL_SYNC_SYNCED
        assert reservation.gcal_event_id in stub.events


def test_cancel_supersedes_pending_create(factory):
    reservation_id = _reservation(factory)
    stub = StubGcalClient()
    with factory() as db:
        reservation = db.get(Reservation, reservation_id)
        reservation.status = RESERVATION_CANCELLED
        reservation.gcal_event_id = "existing-event"
        stub.events["existing-event"] = {"summary": "old"}
        gcal_svc.enqueue_reservation_sync(db, reservation, "cancel", now=_NOW)
        db.commit()

        rows = list(db.execute(select(GcalSyncJob)).scalars())
        assert len(rows) == 1
        assert rows[0].action == GCAL_ACTION_DELETE
        assert gcal_svc.attempt_sync(db, rows[0], client=stub, now=_NOW) == GCAL_SYNC_SYNCED
        db.commit()
        assert reservation.gcal_event_id is None
        assert "existing-event" not in stub.events


def test_max_attempts_becomes_failed_and_can_be_requeued(factory):
    _reservation(factory)
    with factory() as db:
        row = db.execute(select(GcalSyncJob)).scalar_one()
        for attempt in range(gcal_svc.MAX_ATTEMPTS):
            gcal_svc.attempt_sync(
                db,
                row,
                client=_Unavailable(),
                now=_NOW + datetime.timedelta(hours=attempt),
            )
        assert row.status == GCAL_SYNC_FAILED
        assert row.next_attempt_at is None
        assert gcal_svc.retry_failed(db, row.tenant_id, now=_NOW) == 1
        assert row.status == GCAL_SYNC_PENDING
        assert row.attempt_count == 0


def test_http_insert_conflict_is_idempotent(monkeypatch):
    client = gcal_svc.HttpGcalClient()

    def conflict(*args, **kwargs):
        raise gcal_svc.GcalConflict("already exists")

    monkeypatch.setattr(client, "_call", conflict)
    event = {"id": "saas1e2", "summary": "booking"}
    assert client.insert_event(
        calendar_id="primary", refresh_token="secret", event=event
    ) == "saas1e2"


def test_production_without_platform_credentials_never_fake_syncs(
    factory, monkeypatch
):
    monkeypatch.setattr(gcal_svc.settings, "env", "prod")
    monkeypatch.setattr(gcal_svc.settings, "google_oauth_client_id", "")
    monkeypatch.setattr(gcal_svc.settings, "google_oauth_client_secret", "")
    with factory() as db:
        client = gcal_svc.get_gcal_client(db)
        assert isinstance(client, gcal_svc.UnconfiguredGcalClient)
        with pytest.raises(gcal_svc.GcalError, match="平台 Google OAuth"):
            client.insert_event(
                calendar_id="primary", refresh_token="token", event={}
            )


def test_dry_run_does_not_change_job(factory):
    _reservation(factory)
    results = retry_gcal_syncs(
        now=_NOW + datetime.timedelta(minutes=2),
        session_factory=factory,
    )
    assert results and results[0][1] == "would_sync"
    with factory() as db:
        row = db.execute(select(GcalSyncJob)).scalar_one()
        assert row.status == GCAL_SYNC_PENDING
        assert row.attempt_count == 0


def test_unexpected_immediate_error_never_breaks_booking_response(factory, monkeypatch):
    reservation_id = _reservation(factory)

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected adapter failure")

    monkeypatch.setattr(gcal_svc, "attempt_sync", explode)
    with factory() as db:
        assert gcal_svc.attempt_reservation_sync(db, reservation_id) is None
        assert db.is_active

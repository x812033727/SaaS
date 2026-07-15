"""候補排程：dry-run、失敗補送與實際 offer。"""

from __future__ import annotations

import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base, import_all_models
from saas_mvp.line_client import FakeLinePushClient
from saas_mvp.models.booking_slot import BookingSlot
from saas_mvp.models.booking_waitlist import (
    WAITLIST_NOTIFIED,
    WAITLIST_WAITING,
    WaitlistEntry,
)
from saas_mvp.models.line_channel_config import LineChannelConfig
from saas_mvp.models.tenant import Tenant
from saas_mvp.ops.process_waitlists import process_waitlists


import_all_models()
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def test_dry_run_then_apply_offers_waiting_candidate():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    now = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    with _Session() as db:
        tenant = Tenant(name="scheduler_waitlist", plan="free")
        db.add(tenant)
        db.flush()
        cfg = LineChannelConfig(tenant_id=tenant.id, default_target_lang="zh-TW")
        cfg.channel_secret = "s" * 32
        cfg.access_token = "a" * 40
        db.add(cfg)
        slot = BookingSlot(
            tenant_id=tenant.id,
            slot_start=now + datetime.timedelta(days=1),
            max_capacity=1,
        )
        db.add(slot)
        db.flush()
        entry = WaitlistEntry(
            tenant_id=tenant.id,
            slot_id=slot.id,
            line_user_id="Uscheduled",
            status=WAITLIST_WAITING,
            party_size=1,
        )
        db.add(entry)
        db.commit()
        entry_id = entry.id

    dry = process_waitlists(
        session_factory=_Session,
        apply=False,
        now=now,
    )
    assert len(dry) == 1
    assert dry[0].status == "would_check"
    with _Session() as db:
        assert db.get(WaitlistEntry, entry_id).status == WAITLIST_WAITING

    push = FakeLinePushClient()
    applied = process_waitlists(
        session_factory=_Session,
        push_client=push,
        apply=True,
        now=now,
    )
    assert len(applied) == 1
    assert applied[0].status == "offered"
    with _Session() as db:
        row = db.get(WaitlistEntry, entry_id)
        assert row.status == WAITLIST_NOTIFIED
        assert row.offer_expires_at == (
            now + datetime.timedelta(minutes=15)
        ).replace(tzinfo=None)  # SQLite drops timezone metadata
    assert push.call_count == 1
    assert push.sent[0].to_user_id == "Uscheduled"

"""示範資料種子腳本測試 — 注入 in-memory session，驗鍵列建立 + 冪等。

比照 tests/test_booking_reminders.py：自建 in-memory engine + create_all，
把 sessionmaker 注入 seed_demo.run()。重點為冪等（重跑不報錯、不重複租戶）
與關鍵列（location/staff/service/profile slug='demo'）存在，不硬比所有計數。
"""

from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.db import Base, import_all_models
from saas_mvp.models.business_profile import BusinessProfile
from saas_mvp.models.appointment_series import (
    AppointmentSeries,
    AppointmentSeriesOccurrence,
)
from saas_mvp.models.client_form import ClientFormRequest, ClientFormTemplate
from saas_mvp.models.bookable_resource import BookableResource, ResourceType
from saas_mvp.models.location import Location
from saas_mvp.models.service import Service
from saas_mvp.models.staff import Staff
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.ops import seed_demo
from saas_mvp.services import profile as profile_svc

# 確保所有 model 進入 registry（relationship 字串引用 Note 等）。
import_all_models()

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def _fresh_db() -> None:
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)


def _count(model) -> int:
    with _Session() as db:
        return db.execute(select(func.count()).select_from(model)).scalar_one()


def test_seed_creates_demo_tenant_and_key_rows() -> None:
    _fresh_db()
    result = seed_demo.run(session_factory=_Session)

    assert result.created is True
    assert result.user_email == "demo@salon.tw"
    assert result.slug == "demo"

    # 鍵列：租戶 + 使用者 + location/staff/service。
    assert _count(Tenant) == 1
    assert _count(User) == 1
    assert _count(Location) >= 1
    assert _count(Staff) >= 1
    assert _count(Service) >= 1
    assert _count(ClientFormTemplate) == 1
    assert _count(ClientFormRequest) >= 1
    assert _count(AppointmentSeries) == 1
    assert _count(AppointmentSeriesOccurrence) == 4

    # 使用者掛在 demo 租戶上，密碼為雜湊（非明文）。
    with _Session() as db:
        user = db.execute(
            select(User).where(User.email == "demo@salon.tw")
        ).scalar_one()
        assert user.tenant_id == result.tenant_id
        assert user.hashed_password != "demo1234"

    # 公開店家頁 slug='demo' 可解析（已發佈）。
    with _Session() as db:
        prof = profile_svc.get_by_slug(db, "demo")
        assert prof is not None
        assert prof.is_published is True
        assert prof.tenant_id == result.tenant_id


def test_seed_is_idempotent() -> None:
    _fresh_db()
    first = seed_demo.run(session_factory=_Session)

    tenants_after_first = _count(Tenant)
    locations_after_first = _count(Location)
    staff_after_first = _count(Staff)
    services_after_first = _count(Service)
    profiles_after_first = _count(BusinessProfile)
    form_templates_after_first = _count(ClientFormTemplate)
    form_requests_after_first = _count(ClientFormRequest)
    resource_types_after_first = _count(ResourceType)
    resources_after_first = _count(BookableResource)
    series_after_first = _count(AppointmentSeries)
    occurrences_after_first = _count(AppointmentSeriesOccurrence)

    # 重跑不應拋例外。
    second = seed_demo.run(session_factory=_Session)

    assert second.created is False  # 第二次沿用既有租戶
    assert second.tenant_id == first.tenant_id

    # 無重複：核心列數量不變。
    assert _count(Tenant) == tenants_after_first == 1
    assert _count(User) == 1
    assert _count(Location) == locations_after_first
    assert _count(Staff) == staff_after_first
    assert _count(Service) == services_after_first
    assert _count(BusinessProfile) == profiles_after_first == 1
    assert _count(ClientFormTemplate) == form_templates_after_first == 1
    assert _count(ClientFormRequest) == form_requests_after_first
    assert _count(ResourceType) == resource_types_after_first == 1
    assert _count(BookableResource) == resources_after_first == 2
    assert _count(AppointmentSeries) == series_after_first == 1
    assert _count(AppointmentSeriesOccurrence) == occurrences_after_first == 4

    # slug 仍解析到同一租戶。
    with _Session() as db:
        prof = profile_svc.get_by_slug(db, "demo")
        assert prof is not None
        assert prof.tenant_id == first.tenant_id


def test_seed_custom_email_and_tenant_name() -> None:
    _fresh_db()
    result = seed_demo.run(
        session_factory=_Session,
        email="owner@demo.tw",
        password="s3cret99",
        tenant_name="測試髮廊",
    )
    assert result.user_email == "owner@demo.tw"
    assert result.tenant_name == "測試髮廊"
    with _Session() as db:
        tenant = db.get(Tenant, result.tenant_id)
        assert tenant.name == "測試髮廊"

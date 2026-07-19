"""公開常駐網路預約(R12-A)測試。

覆蓋:
- 入口三閘:opt-in / WEB_BOOKING feature / is_published 任一未過 → 404
- 三步 GET 流程(服務→日期→時段+身分欄位)
- POST 建單:建 walk-in 顧客(電話正規化+email)、note=網路預約、portal 連結
- 電話去重:同電話(不同格式)併檔既有 walk-in 客、不覆寫姓名/email、不發 portal
- LINE 客同電話不併檔(另建 walk-in);黑名單 walk-in 客拒絕
- 驗證:缺姓名/壞電話 → 錯誤頁不建單;額滿 → 友善錯誤
- 防灌單:每租戶 30/hr 上限
- 公開店家頁 CTA:開啟才渲染「線上預約」
"""

from __future__ import annotations

import datetime
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SAAS_RATE_LIMIT_ENABLED", "false")

from saas_mvp.models import tenant as _t, user as _u  # noqa: F401,E402
from saas_mvp.models import customer as _c, booking_slot as _bs  # noqa: F401,E402
from saas_mvp.models import reservation as _r  # noqa: F401,E402
from saas_mvp.models import business_profile as _bp  # noqa: F401,E402

from saas_mvp.app import create_app  # noqa: E402
from saas_mvp.db import Base, get_db  # noqa: E402
from saas_mvp.models.booking_slot import BookingSlot  # noqa: E402
from saas_mvp.models.business_profile import BusinessProfile  # noqa: E402
from saas_mvp.models.customer import Customer  # noqa: E402
from saas_mvp.models.reservation import Reservation  # noqa: E402
from saas_mvp.models.service import Service  # noqa: E402
from saas_mvp.models.tenant import Tenant  # noqa: E402
from saas_mvp.services import features as features_svc  # noqa: E402
from saas_mvp.services import public_booking as pb_svc  # noqa: E402

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

_SLOT_START = datetime.datetime(2030, 6, 1, 18, 0, tzinfo=datetime.timezone.utc)
_DATE = "2030-06-01"


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    app = create_app()

    def override_db():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _seed(
    *,
    published=True,
    opt_in=True,
    feature=True,
    capacity=4,
    with_service=True,
):
    """回傳 (tenant_id, slug, service_id, slot_id)。"""
    db = _Session()
    try:
        t = Tenant(name=f"pb_{uuid.uuid4().hex[:8]}", plan="free")
        db.add(t)
        db.flush()
        slug = f"shop-{uuid.uuid4().hex[:8]}"
        db.add(
            BusinessProfile(
                tenant_id=t.id,
                slug=slug,
                display_name="測試小店",
                is_published=published,
                online_booking_enabled=opt_in,
            )
        )
        service_id = None
        if with_service:
            svc = Service(
                tenant_id=t.id, name="剪髮", duration_minutes=60, price_cents=80000
            )
            db.add(svc)
            db.flush()
            service_id = svc.id
        slot = BookingSlot(
            tenant_id=t.id,
            slot_start=_SLOT_START,
            slot_end=_SLOT_START + datetime.timedelta(hours=2),
            max_capacity=capacity,
        )
        db.add(slot)
        db.flush()
        slot_id = slot.id
        if feature:
            features_svc.set_enabled(
                db, t.id, features_svc.WEB_BOOKING, True,
                actor_user_id=None, source="test",
            )
        else:
            features_svc.set_enabled(
                db, t.id, features_svc.WEB_BOOKING, False,
                actor_user_id=None, source="test",
            )
        db.commit()
        return t.id, slug, service_id, slot_id
    finally:
        db.close()


def _post_booking(client, slug, slot_id, *, service_id=None, **overrides):
    data = {
        "slot_id": slot_id,
        "party_size": 1,
        "name": "王小明",
        "phone": "0912-345-678",
        "email": "",
    }
    if service_id is not None:
        data["service_id"] = service_id
    data.update(overrides)
    return client.post(f"/p/{slug}/book", data=data)


# ── 入口三閘 ─────────────────────────────────────────────────────────────────

class TestEntryGates:
    def test_all_gates_pass(self, client):
        _tid, slug, _sid, _slot = _seed()
        assert client.get(f"/p/{slug}/book").status_code == 200

    def test_unpublished_404(self, client):
        _tid, slug, _sid, _slot = _seed(published=False)
        assert client.get(f"/p/{slug}/book").status_code == 404

    def test_opt_in_off_404(self, client):
        _tid, slug, _sid, _slot = _seed(opt_in=False)
        assert client.get(f"/p/{slug}/book").status_code == 404

    def test_feature_off_404(self, client):
        _tid, slug, _sid, _slot = _seed(feature=False)
        assert client.get(f"/p/{slug}/book").status_code == 404

    def test_unknown_slug_404(self, client):
        assert client.get("/p/no-such-shop/book").status_code == 404

    def test_post_also_gated(self, client):
        _tid, slug, _sid, slot_id = _seed(opt_in=False)
        assert _post_booking(client, slug, slot_id).status_code == 404


# ── 三步流程與建單 ───────────────────────────────────────────────────────────

class TestFlow:
    def test_three_steps(self, client):
        _tid, slug, sid, _slot = _seed()
        r1 = client.get(f"/p/{slug}/book")
        assert "選擇服務" in r1.text and "剪髮" in r1.text
        r2 = client.get(f"/p/{slug}/book", params={"service_id": sid})
        assert "選擇日期" in r2.text and _DATE in r2.text
        r3 = client.get(f"/p/{slug}/book", params={"service_id": sid, "date": _DATE})
        assert "選擇時段" in r3.text
        assert 'name="name"' in r3.text and 'name="phone"' in r3.text

    def test_no_service_goes_straight_to_dates(self, client):
        _tid, slug, _sid, _slot = _seed(with_service=False)
        r = client.get(f"/p/{slug}/book")
        assert "選擇日期" in r.text

    def test_booking_creates_walkin_customer(self, client, monkeypatch):
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "public_base_url", "https://t.example")
        tid, slug, sid, slot_id = _seed()
        r = _post_booking(
            client, slug, slot_id, service_id=sid, email="ming@example.com"
        )
        assert r.status_code == 200 and "預約完成" in r.text
        # 新建檔 → 有 portal 連結
        assert "/booking/my/" in r.text
        db = _Session()
        try:
            customer = (
                db.query(Customer).filter(Customer.tenant_id == tid).one()
            )
            assert customer.line_user_id is None
            assert customer.display_name == "王小明"
            assert customer.phone == "0912345678"  # 正規化去分隔符
            assert customer.email == "ming@example.com"
            assert customer.booking_count == 1
            resv = (
                db.query(Reservation).filter(Reservation.tenant_id == tid).one()
            )
            assert resv.customer_id == customer.id
            assert resv.note == pb_svc.WEB_BOOKING_NOTE
        finally:
            db.close()

    def test_phone_dedup_reuses_walkin_no_overwrite(self, client, monkeypatch):
        from saas_mvp.config import settings

        monkeypatch.setattr(settings, "public_base_url", "https://t.example")
        tid, slug, sid, slot_id = _seed()
        _post_booking(client, slug, slot_id, service_id=sid, email="a@example.com")
        # 同電話不同格式 + 不同姓名/email → 併檔、不覆寫、不發 portal
        r = _post_booking(
            client, slug, slot_id, service_id=sid,
            name="假名", phone="+886 912 345 678", email="evil@example.com",
        )
        assert r.status_code == 200 and "預約完成" in r.text
        assert "/booking/my/" not in r.text
        db = _Session()
        try:
            customers = db.query(Customer).filter(Customer.tenant_id == tid).all()
            assert len(customers) == 1
            c = customers[0]
            assert c.display_name == "王小明"  # 不被第二次輸入覆寫
            assert c.email == "a@example.com"
            assert c.booking_count == 2
        finally:
            db.close()

    def test_line_customer_same_phone_not_matched(self, client):
        tid, slug, sid, slot_id = _seed()
        db = _Session()
        try:
            db.add(
                Customer(
                    tenant_id=tid, line_user_id="Uline1",
                    display_name="LINE客", phone="0912345678",
                )
            )
            db.commit()
        finally:
            db.close()
        r = _post_booking(client, slug, slot_id, service_id=sid)
        assert r.status_code == 200 and "預約完成" in r.text
        db = _Session()
        try:
            walkins = (
                db.query(Customer)
                .filter(
                    Customer.tenant_id == tid, Customer.line_user_id.is_(None)
                )
                .all()
            )
            assert len(walkins) == 1  # 另建 walk-in,不掛到 LINE 客
        finally:
            db.close()

    def test_blacklisted_line_customer_same_phone_rejected(self, client):
        # 對抗審查缺陷 1:被拉黑的 LINE 客改走匿名管道用同支電話重約,
        # 必須被擋(檢查跨所有電話相符客,不限 walk-in)。
        tid, slug, sid, slot_id = _seed()
        db = _Session()
        try:
            db.add(
                Customer(
                    tenant_id=tid, line_user_id="Ubad1",
                    display_name="黑名單LINE客", phone="0912345678",
                    blacklisted=True,
                )
            )
            db.commit()
        finally:
            db.close()
        r = _post_booking(client, slug, slot_id, service_id=sid)
        assert "請直接與店家聯繫" in r.text
        db = _Session()
        try:
            assert (
                db.query(Reservation).filter(Reservation.tenant_id == tid).count()
                == 0
            )
            # 也不得留下新建的 walk-in 檔
            assert (
                db.query(Customer)
                .filter(Customer.tenant_id == tid, Customer.line_user_id.is_(None))
                .count()
                == 0
            )
        finally:
            db.close()

    def test_deposit_policy_applies_to_web_booking(self, client):
        # 對抗審查缺陷 2:匿名管道原本 line_user_id=None → 定金被靜默跳過。
        tid, slug, sid, slot_id = _seed()
        db = _Session()
        try:
            t = db.get(Tenant, tid)
            t.deposit_cents = 20000
            features_svc.set_enabled(
                db, tid, features_svc.DEPOSIT_PAYMENT, True,
                actor_user_id=None, source="test",
            )
            db.commit()
        finally:
            db.close()
        r = _post_booking(client, slug, slot_id, service_id=sid)
        assert r.status_code == 200 and "預約完成" in r.text
        assert "前往付定金" in r.text
        db = _Session()
        try:
            resv = db.query(Reservation).filter(Reservation.tenant_id == tid).one()
            assert resv.deposit_status == "pending"
            assert resv.deposit_cents == 20000
        finally:
            db.close()

    def test_party_size_clamped_server_side(self, client):
        tid, slug, sid, slot_id = _seed(capacity=100)
        r = _post_booking(
            client, slug, slot_id, service_id=sid, party_size=9999
        )
        assert r.status_code == 200 and "預約完成" in r.text
        db = _Session()
        try:
            resv = db.query(Reservation).filter(Reservation.tenant_id == tid).one()
            assert resv.party_size == 6
        finally:
            db.close()

    def test_blacklisted_walkin_rejected(self, client):
        tid, slug, sid, slot_id = _seed()
        db = _Session()
        try:
            db.add(
                Customer(
                    tenant_id=tid, line_user_id=None,
                    display_name="黑名單", phone="0912345678",
                    blacklisted=True,
                )
            )
            db.commit()
        finally:
            db.close()
        r = _post_booking(client, slug, slot_id, service_id=sid)
        assert "請直接與店家聯繫" in r.text
        db = _Session()
        try:
            assert (
                db.query(Reservation).filter(Reservation.tenant_id == tid).count()
                == 0
            )
        finally:
            db.close()


# ── 驗證與防護 ───────────────────────────────────────────────────────────────

class TestValidation:
    def test_missing_name_rejected(self, client):
        tid, slug, sid, slot_id = _seed()
        r = _post_booking(client, slug, slot_id, service_id=sid, name="  ")
        assert "請填寫姓名" in r.text
        db = _Session()
        try:
            assert db.query(Reservation).filter(Reservation.tenant_id == tid).count() == 0
        finally:
            db.close()

    def test_bad_phone_rejected(self, client):
        _tid, slug, sid, slot_id = _seed()
        r = _post_booking(client, slug, slot_id, service_id=sid, phone="abc")
        assert "請填寫正確的聯絡電話" in r.text

    def test_bad_email_rejected(self, client):
        _tid, slug, sid, slot_id = _seed()
        r = _post_booking(client, slug, slot_id, service_id=sid, email="not-an-email")
        assert "Email 格式不正確" in r.text

    def test_slot_full_friendly_error(self, client):
        _tid, slug, sid, slot_id = _seed(capacity=1)
        _post_booking(client, slug, slot_id, service_id=sid)
        r = _post_booking(
            client, slug, slot_id, service_id=sid, phone="0987654321"
        )
        assert "額滿" in r.text

    def test_flood_gate_30_per_hour(self, client):
        tid, slug, sid, slot_id = _seed(capacity=100)
        now = datetime.datetime.now(datetime.timezone.utc)
        db = _Session()
        try:
            for i in range(30):
                db.add(
                    Reservation(
                        tenant_id=tid, slot_id=slot_id, party_size=1,
                        note=pb_svc.WEB_BOOKING_NOTE, created_at=now,
                    )
                )
            db.commit()
        finally:
            db.close()
        r = _post_booking(client, slug, slot_id, service_id=sid)
        assert "請稍後再試" in r.text


# ── 公開店家頁 CTA ───────────────────────────────────────────────────────────

class TestProfileCta:
    def test_cta_rendered_when_enabled(self, client):
        _tid, slug, _sid, _slot = _seed()
        r = client.get(f"/p/{slug}")
        assert f"/p/{slug}/book" in r.text

    def test_cta_hidden_when_opt_in_off(self, client):
        _tid, slug, _sid, _slot = _seed(opt_in=False)
        r = client.get(f"/p/{slug}")
        assert r.status_code == 200
        assert f"/p/{slug}/book" not in r.text


# ── 電話正規化單元 ───────────────────────────────────────────────────────────

class TestPhoneNormalize:
    def test_variants(self):
        assert pb_svc.normalize_phone("0912-345-678") == "0912345678"
        assert pb_svc.normalize_phone("+886 912 345 678") == "0912345678"
        assert pb_svc.normalize_phone("886912345678") == "0912345678"
        assert pb_svc.normalize_phone("(02) 2345 6789") == "0223456789"

    def test_rejects_garbage(self):
        for bad in ["abc", "123", "", "09123456789012345", "0912a45678"]:
            with pytest.raises(pb_svc.PublicBookingError):
                pb_svc.normalize_phone(bad)

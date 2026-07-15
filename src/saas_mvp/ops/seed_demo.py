"""建立／刷新示範租戶，讓人能登入 /ui 點過每一項功能（idempotent）。

Usage:
    python -m saas_mvp.ops.seed_demo
    python -m saas_mvp.ops.seed_demo --email demo@salon.tw --password demo1234
    python -m saas_mvp.ops.seed_demo --reset           # 既有 demo 租戶就地刷新/略過
    SAAS_DATABASE_URL=sqlite:////tmp/demo.db python -m saas_mvp.ops.seed_demo

設計（比照 ops/send_due_reminders.py）：
  * argparse + 可注入 session_factory（供測試以 in-memory session 跑 run()）。
  * 走既有 service 層（不繞過商業規則）：tenant/user 比照 /auth/register 的
    model 路徑建立、features.set_enabled 開通旗標、locations/staff/catalog/slots/
    booking/shop/coupons/flex_menu/portfolio/profile/faq/marketing 各服務建樣本。
  * **冪等**：以 email/租戶名查找既有 demo，存在則沿用其 id 並 skip-or-update 各樣本
    （以名稱/代碼/slug 等自然鍵判存在），重跑兩次不報錯、不重複。
  * 結尾印出登入 URL、帳密、公開店家頁、員工入口連結。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from dataclasses import dataclass, field
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from saas_mvp.auth.security import hash_password
from saas_mvp.config import settings
from saas_mvp.db import SessionLocal, import_all_models, init_db
from saas_mvp.models.campaign import CAMPAIGN_BIRTHDAY, Campaign
from saas_mvp.models.appointment_series import AppointmentSeries
from saas_mvp.models.coupon import DISCOUNT_AMOUNT, DISCOUNT_PERCENT
from saas_mvp.models.customer import upsert_customer_from_line
from saas_mvp.models.reservation import Reservation
from saas_mvp.models.staff import Staff
from saas_mvp.models.tenant import Tenant
from saas_mvp.models.user import User
from saas_mvp.services import booking as booking_svc
from saas_mvp.services import appointment_series as appointment_series_svc
from saas_mvp.services import bookable_resources as resources_svc
from saas_mvp.services import catalog as catalog_svc
from saas_mvp.services import client_forms as client_forms_svc
from saas_mvp.services import coupons as coupons_svc
from saas_mvp.services import faq as faq_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import flex_menu as flex_menu_svc
from saas_mvp.services import locations as locations_svc
from saas_mvp.services import membership as membership_svc
from saas_mvp.services import portfolio as portfolio_svc
from saas_mvp.services import profile as profile_svc
from saas_mvp.services import shop as shop_svc
from saas_mvp.services import slots as slots_svc
from saas_mvp.services import staff as staff_svc

DEMO_SLUG = "demo"
DEMO_LINE_USER_ID = "Udemo0000000000000000000000000001"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class SeedResult:
    """run() 回傳：供測試斷言 + 印摘要。"""

    tenant_id: int
    user_email: str
    password: str
    tenant_name: str
    slug: str
    created: bool  # True=新建租戶；False=沿用既有（idempotent 重跑）
    staff_token: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def login_url(self) -> str:
        return f"{_base_url()}/ui/login"

    def profile_url(self) -> str:
        return f"{_base_url()}/p/{self.slug}"

    def staff_portal_url(self) -> str | None:
        if not self.staff_token:
            return None
        return f"{_base_url()}/s/{self.staff_token}"


def _base_url() -> str:
    return (settings.public_base_url or "").rstrip("/") or "http://127.0.0.1:8000"


# ─────────────────────────────────────────────────────────────────────────────
# 租戶 + 使用者（比照 routers/auth.py 的 register model 路徑）
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_tenant_user(
    db: Session, *, email: str, password: str, tenant_name: str
) -> tuple[Tenant, User, bool]:
    """查找既有 demo（依 email 或租戶名），無則新建。回傳 (tenant, user, created)。"""
    user = db.execute(
        select(User).where(User.email == email)
    ).scalar_one_or_none()
    if user is not None:
        tenant = db.get(Tenant, user.tenant_id)
        return tenant, user, False

    tenant = db.execute(
        select(Tenant).where(Tenant.name == tenant_name)
    ).scalar_one_or_none()
    created = False
    if tenant is None:
        tenant = Tenant(name=tenant_name, plan="pro", store_type="service")
        db.add(tenant)
        db.flush()
        created = True

    user = User(
        email=email,
        hashed_password=hash_password(password),
        tenant_id=tenant.id,
    )
    db.add(user)
    db.flush()
    from saas_mvp.services import organizations as organizations_svc

    organizations_svc.ensure_user_memberships(db, tenant=tenant, user=user)
    return tenant, user, created


def _enable_all_features(db: Session, tenant_id: int, *, actor_user_id: int) -> int:
    """開通全部進階旗標（idempotent：set_enabled 為 upsert）。"""
    for feature in sorted(features_svc.VALID_FEATURES):
        features_svc.set_enabled(
            db,
            tenant_id,
            feature,
            True,
            actor_user_id=actor_user_id,
            source="admin",
            reason="seed_demo",
        )
    return len(features_svc.VALID_FEATURES)


# ─────────────────────────────────────────────────────────────────────────────
# 樣本資料（每段以自然鍵判存在 → skip-or-reuse，保證冪等）
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_locations(db: Session, tenant_id: int) -> list:
    want = [
        ("信義店", "台北市信義區松壽路 1 號", "02-1234-5678"),
        ("西門店", "台北市萬華區漢中街 50 號", "02-2345-6789"),
    ]
    existing = {
        loc.name: loc for loc in locations_svc.list_locations(db, tenant_id=tenant_id)
    }
    out = []
    for name, address, phone in want:
        loc = existing.get(name)
        if loc is None:
            loc = locations_svc.create_location(
                db, tenant_id=tenant_id, name=name, address=address, phone=phone
            )
        out.append(loc)
    return out


def _ensure_staff(db: Session, tenant_id: int, location_id: int) -> list[Staff]:
    want = [
        ("設計師A", "designer"),
        ("設計師B", "designer"),
        ("助理C", "assistant"),
    ]
    existing = {
        s.name: s for s in staff_svc.list_staff(db, tenant_id=tenant_id)
    }
    out: list[Staff] = []
    for name, role in want:
        member = existing.get(name)
        if member is None:
            member = staff_svc.create_staff(
                db, tenant_id=tenant_id, name=name, role=role,
                location_id=location_id,
            )
        out.append(member)

    # 每人週一/週三班表（依 staff/weekday/start_time 自然唯一）。
    for member in out:
        have = {
            (sh.weekday, sh.start_time)
            for sh in staff_svc.list_shifts(db, tenant_id=tenant_id, staff_id=member.id)
        }
        for weekday in (0, 2):  # Mon, Wed
            if (weekday, "10:00") not in have:
                staff_svc.create_shift(
                    db, tenant_id=tenant_id, staff_id=member.id,
                    weekday=weekday, start_time="10:00", end_time="18:00",
                )

    # 第一位設計師排一筆已核准休假（無既有則新增）。
    lead = out[0]
    if not staff_svc.list_leaves(db, tenant_id=tenant_id, staff_id=lead.id):
        start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0) \
            + datetime.timedelta(days=14)
        staff_svc.create_leave(
            db, tenant_id=tenant_id, staff_id=lead.id,
            start_at=start, end_at=start + datetime.timedelta(days=1),
            reason="特休",
        )
    return out


def _ensure_catalog(db: Session, tenant_id: int, staff: list[Staff]) -> list:
    cats_want = ["剪髮", "染髮", "護髮"]
    existing_cats = {
        c.name: c for c in catalog_svc.list_categories(db, tenant_id=tenant_id)
    }
    cats = {}
    for name in cats_want:
        c = existing_cats.get(name)
        if c is None:
            c = catalog_svc.create_category(db, tenant_id=tenant_id, name=name)
        cats[name] = c

    svc_want = [
        ("精緻剪髮", "剪髮", 45, 60000),
        ("時尚染髮", "染髮", 120, 180000),
        ("深層護髮", "護髮", 60, 90000),
    ]
    existing_svcs = {
        s.name: s for s in catalog_svc.list_services(db, tenant_id=tenant_id)
    }
    out = []
    for name, cat_name, dur, price in svc_want:
        s = existing_svcs.get(name)
        if s is None:
            s = catalog_svc.create_service(
                db, tenant_id=tenant_id, name=name,
                category_id=cats[cat_name].id,
                duration_minutes=dur, price_cents=price,
            )
            # 指派前兩位設計師（idempotent：僅新建的服務指派）。
            for member in staff[:2]:
                catalog_svc.assign_staff(
                    db, tenant_id=tenant_id, service_id=s.id, staff_id=member.id
                )
        out.append(s)
    return out


def _ensure_resources(db: Session, tenant_id: int, services: list) -> int:
    """建立護髮座位與服務需求，讓示範預約可看到自動配置。"""
    resource_types = {
        row.name: row for row in resources_svc.list_types(db, tenant_id=tenant_id)
    }
    resource_type = resource_types.get("護髮座位")
    if resource_type is None:
        resource_type = resources_svc.create_type(
            db,
            tenant_id=tenant_id,
            name="護髮座位",
            description="深層護髮服務自動配置的座位",
        )
    existing = {
        row.name for row in resources_svc.list_resources(db, tenant_id=tenant_id)
    }
    for name, code in (("護髮座位 A", "TREAT-A"), ("護髮座位 B", "TREAT-B")):
        if name not in existing:
            resources_svc.create_resource(
                db,
                tenant_id=tenant_id,
                resource_type_id=resource_type.id,
                name=name,
                internal_code=code,
                capacity=1,
            )
    target = next((service for service in services if service.name == "深層護髮"), None)
    if target is not None:
        resources_svc.set_requirement(
            db,
            tenant_id=tenant_id,
            service_id=target.id,
            resource_type_id=resource_type.id,
            quantity=1,
        )
    return len(resources_svc.list_resources(db, tenant_id=tenant_id))


def _ensure_slots_and_reservations(
    db: Session, tenant_id: int, services: list
) -> tuple[int, int]:
    """未來數日各建一個時段，並對前兩個時段各下一筆預約。冪等：以既有時段數判斷。"""
    base = _utcnow().replace(minute=0, second=0, microsecond=0) \
        + datetime.timedelta(days=1)
    starts = [base + datetime.timedelta(days=i, hours=i) for i in range(3)]

    existing_starts = {
        s.slot_start.replace(tzinfo=None) if s.slot_start.tzinfo else s.slot_start
        for s in slots_svc.list_slots(db, tenant_id=tenant_id)
    }
    slots = []
    for start in starts:
        naive = start.replace(tzinfo=None)
        if naive in existing_starts or start in existing_starts:
            # 找回既有那個時段
            match = next(
                (
                    s for s in slots_svc.list_slots(db, tenant_id=tenant_id)
                    if (s.slot_start.replace(tzinfo=None) == naive)
                ),
                None,
            )
            if match is not None:
                slots.append(match)
            continue
        slot = slots_svc.create_slot(
            db, tenant_id=tenant_id, slot_start=start, max_capacity=4,
        )
        slots.append(slot)

    # 對前兩個時段各下一筆預約（若該時段尚無預約才下，避免重跑累加）。
    resv_count = 0
    demo_service = next(
        (service for service in services if service.name == "深層護髮"),
        services[0] if services else None,
    )
    for slot in slots[:2]:
        has = db.execute(
            select(Reservation).where(Reservation.slot_id == slot.id)
        ).first()
        if has is not None:
            continue
        booking_svc.book_slot(
            db, tenant_id=tenant_id, slot_id=slot.id, party_size=1,
            line_user_id=DEMO_LINE_USER_ID, display_name="示範顧客",
            service_id=demo_service.id if demo_service is not None else None,
        )
        resv_count += 1
    return len(slots), resv_count


def _ensure_appointment_series(
    db: Session, tenant_id: int, actor_user_id: int
) -> int:
    """建立一組每週療程，讓示範後台可直接操作系列取消與衝突狀態。"""
    existing = db.execute(
        select(AppointmentSeries)
        .where(AppointmentSeries.tenant_id == tenant_id)
        .order_by(AppointmentSeries.id)
    ).scalars().first()
    if existing is not None:
        return 1
    source = db.execute(
        select(Reservation)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == "confirmed",
        )
        .order_by(Reservation.id)
    ).scalars().first()
    if source is None:
        return 0
    appointment_series_svc.create_from_reservation(
        db,
        tenant_id=tenant_id,
        reservation_id=source.id,
        recurrence_unit="week",
        recurrence_interval=1,
        occurrence_count=4,
        auto_create_slots=True,
        actor_user_id=actor_user_id,
    )
    return 1


def _ensure_products(db: Session, tenant_id: int) -> int:
    want = [
        ("沙龍級洗髮精", 48000, 50),
        ("造型髮蠟", 32000, 80),
    ]
    existing = {
        p.name for p in shop_svc.list_products(db, tenant_id=tenant_id)
    }
    n = 0
    for name, price, stock in want:
        if name in existing:
            continue
        shop_svc.create_product(
            db, tenant_id=tenant_id, name=name, price_cents=price, stock=stock,
        )
        n += 1
    return n


def _ensure_coupons(db: Session, tenant_id: int) -> int:
    want = [
        ("WELCOME10", "新客 9 折", DISCOUNT_PERCENT, 10),
        ("CASH100", "折抵 NT$100", DISCOUNT_AMOUNT, 10000),
    ]
    existing = {c.code for c in coupons_svc.list_coupons(db, tenant_id=tenant_id)}
    n = 0
    for code, name, dtype, value in want:
        if code in existing:
            continue
        coupons_svc.create_coupon(
            db, tenant_id=tenant_id, code=code, name=name,
            discount_type=dtype, discount_value=value,
        )
        n += 1
    return n


def _ensure_flex_menu(db: Session, tenant_id: int) -> int:
    menu = flex_menu_svc.get_active_menu(db, tenant_id=tenant_id)
    if menu is None:
        menus = flex_menu_svc.list_menus(db, tenant_id=tenant_id)
        menu = menus[0] if menus else None
    if menu is None:
        menu = flex_menu_svc.create_menu(db, tenant_id=tenant_id, title="示範圖文選單")
    existing_titles = {
        c.title for c in flex_menu_svc.list_cards(db, tenant_id=tenant_id, menu_id=menu.id)
    }
    cards = [
        ("立即預約", "message", "預約"),
        ("我的預約", "message", "我的預約"),
        ("店家介紹", "uri", f"{_base_url()}/p/{DEMO_SLUG}"),
    ]
    n = 0
    for i, (title, action_type, action_data) in enumerate(cards):
        if title in existing_titles:
            continue
        flex_menu_svc.add_card(
            db, tenant_id=tenant_id, menu_id=menu.id, title=title,
            action_type=action_type, action_data=action_data, sort_order=i,
        )
        n += 1
    return n


def _ensure_portfolio(db: Session, tenant_id: int) -> int:
    cats = portfolio_svc.list_categories(db, tenant_id=tenant_id)
    cat = next((c for c in cats if c.name == "作品精選"), None)
    if cat is None:
        cat = portfolio_svc.create_category(db, tenant_id=tenant_id, name="作品精選")
    existing = {it.image_url for it in portfolio_svc.list_items(db, tenant_id=tenant_id)}
    items = [
        ("https://placehold.co/600x600?text=Style+1", "韓系層次"),
        ("https://placehold.co/600x600?text=Style+2", "霧感染髮"),
    ]
    n = 0
    for url, caption in items:
        if url in existing:
            continue
        portfolio_svc.create_item(
            db, tenant_id=tenant_id, image_url=url, category_id=cat.id, caption=caption
        )
        n += 1
    return n


def _ensure_profile(db: Session, tenant_id: int, tenant_name: str) -> str:
    existing = profile_svc.get_by_tenant(db, tenant_id)
    if existing is not None and existing.slug == DEMO_SLUG and existing.is_published:
        return existing.slug
    prof = profile_svc.upsert(
        db,
        tenant_id,
        slug=DEMO_SLUG,
        is_published=True,
        display_name=tenant_name,
        intro="這是一個示範店家頁，展示線上預約、作品集與優惠資訊。",
        theme_color="#06c755",
    )
    return prof.slug


def _ensure_faq(db: Session, tenant_id: int) -> int:
    want = [
        ("營業時間？", "週一至週日 10:00–20:00。"),
        ("如何預約？", "於 LINE 點選「立即預約」，或來電 02-1234-5678。"),
        ("可以取消嗎？", "預約前一天可於 LINE 輸入「取消 編號」自助取消。"),
    ]
    existing = {f.question for f in faq_svc.list_faqs(db, tenant_id=tenant_id)}
    n = 0
    for i, (q, a) in enumerate(want):
        if q in existing:
            continue
        faq_svc.create_faq(db, tenant_id=tenant_id, question=q, answer=a, sort_order=i)
        n += 1
    return n


def _ensure_campaign(db: Session, tenant_id: int) -> int:
    existing = db.execute(
        select(Campaign).where(
            Campaign.tenant_id == tenant_id,
            Campaign.type == CAMPAIGN_BIRTHDAY,
            Campaign.name == "生日好禮",
        )
    ).first()
    if existing is not None:
        return 0
    campaign = Campaign(
        tenant_id=tenant_id,
        name="生日好禮",
        type=CAMPAIGN_BIRTHDAY,
        status="active",
        is_active=True,
        message_template="親愛的顧客，生日快樂！本月來店即贈護髮乙次 🎂",
        reward_type="points",
        reward_value=100,
    )
    db.add(campaign)
    db.flush()
    return 1


def _ensure_customer(db: Session, tenant_id: int) -> int:
    """有電話 + 生日 + 點數的顧客（POS 查詢用）。冪等：以 line_user_id upsert。"""
    customer = upsert_customer_from_line(
        db, tenant_id=tenant_id, line_user_id=DEMO_LINE_USER_ID,
        display_name="示範顧客", bump_booking=False,
    )
    db.flush()
    customer.phone = "0912345678"
    customer.birthday = datetime.date(1995, 6, 22)
    if (customer.points_balance or 0) < 100:
        membership_svc.earn_points(
            db, tenant_id=tenant_id, customer=customer,
            delta=100 - (customer.points_balance or 0), reason="seed_demo",
        )
    return 1


def _ensure_client_form(db: Session, tenant_id: int) -> int:
    """建立可直接展示的全服務諮詢表，並補到示範預約（冪等）。"""
    name = "服務前健康諮詢與同意書"
    template = next(
        (
            row
            for row in client_forms_svc.list_templates(db, tenant_id=tenant_id)
            if row.name == name
        ),
        None,
    )
    if template is None:
        template = client_forms_svc.create_template(
            db,
            tenant_id=tenant_id,
            name=name,
            intro="請於服務前如實填寫；送出後會保存於本次預約紀錄。",
            consent_text="本人確認上述資料正確，並已了解服務內容、可能風險與注意事項。",
            service_id=None,
            require_signature=True,
        )
        client_forms_svc.add_question(
            db,
            tenant_id=tenant_id,
            template_id=template.id,
            label="是否有藥物或成分過敏史？",
            field_type="select",
            required=True,
            options="沒有\n有，將於備註說明",
        )
        client_forms_svc.add_question(
            db,
            tenant_id=tenant_id,
            template_id=template.id,
            label="需要店家特別留意的事項",
            field_type="textarea",
            required=False,
        )
        client_forms_svc.add_question(
            db,
            tenant_id=tenant_id,
            template_id=template.id,
            label="以上資料均為本人如實填寫",
            field_type="checkbox",
            required=True,
        )
        client_forms_svc.set_active(
            db, tenant_id=tenant_id, template_id=template.id, active=True
        )

    # 測試與正式 session 都關閉 autoflush；先落下 active/問題狀態，派發查詢才看得到。
    db.flush()
    reservations = db.execute(
        select(Reservation).where(Reservation.tenant_id == tenant_id)
    ).scalars()
    for reservation in reservations:
        client_forms_svc.attach_to_reservation(db, reservation=reservation)
    return 1


# ─────────────────────────────────────────────────────────────────────────────
def run(
    *,
    session_factory: sessionmaker = SessionLocal,
    email: str = "demo@salon.tw",
    password: str = "demo1234",
    tenant_name: str = "示範美髮沙龍",
    reset: bool = False,
) -> SeedResult:
    """建立／刷新示範租戶與全部樣本資料（idempotent）。回傳 SeedResult。"""
    # 確保 ORM 註冊表完整（standalone 入口未經 app router import 全部 model）。
    import_all_models()
    with session_factory() as db:
        tenant, user, created = _ensure_tenant_user(
            db, email=email, password=password, tenant_name=tenant_name
        )
        tenant_id = tenant.id
        user_id = user.id

        counts: dict[str, int] = {}
        counts["features"] = _enable_all_features(db, tenant_id, actor_user_id=user_id)

        locations = _ensure_locations(db, tenant_id)
        counts["locations"] = len(locations)

        staff = _ensure_staff(db, tenant_id, locations[0].id)
        counts["staff"] = len(staff)

        services = _ensure_catalog(db, tenant_id, staff)
        counts["services"] = len(services)
        counts["resources"] = _ensure_resources(db, tenant_id, services)

        n_slots, n_resv = _ensure_slots_and_reservations(db, tenant_id, services)
        counts["slots"] = n_slots
        counts["reservations"] = n_resv
        counts["appointment_series"] = _ensure_appointment_series(
            db, tenant_id, user_id
        )

        counts["products"] = _ensure_products(db, tenant_id)
        counts["coupons"] = _ensure_coupons(db, tenant_id)
        counts["flex_cards"] = _ensure_flex_menu(db, tenant_id)
        counts["portfolio"] = _ensure_portfolio(db, tenant_id)
        slug = _ensure_profile(db, tenant_id, tenant_name)
        counts["faq"] = _ensure_faq(db, tenant_id)
        counts["campaigns"] = _ensure_campaign(db, tenant_id)
        counts["customers"] = _ensure_customer(db, tenant_id)
        counts["client_forms"] = _ensure_client_form(db, tenant_id)

        # 取一個員工 access_token 給員工入口連結。
        staff_token = next(
            (s.access_token for s in staff if s.access_token), None
        )

        db.commit()

        return SeedResult(
            tenant_id=tenant_id,
            user_email=email,
            password=password,
            tenant_name=tenant_name,
            slug=slug,
            created=created,
            staff_token=staff_token,
            counts=counts,
        )


def write_report(result: SeedResult, *, out: TextIO) -> None:
    print(
        "created" if result.created else "reused",
        f"tenant_id={result.tenant_id}",
        file=out,
    )
    for key in sorted(result.counts):
        print(f"  {key}={result.counts[key]}", file=out)
    print("", file=out)
    print("=== 示範環境就緒 ===", file=out)
    print(f"管理後台登入：{result.login_url()}", file=out)
    print(f"  帳號：{result.user_email}", file=out)
    print(f"  密碼：{result.password}", file=out)
    print(f"公開店家頁：{result.profile_url()}", file=out)
    portal = result.staff_portal_url()
    if portal:
        print(f"員工入口：{portal}", file=out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed a demo tenant so a person can click through every /ui feature.",
    )
    parser.add_argument("--email", default="demo@salon.tw", help="Demo login email.")
    parser.add_argument("--password", default="demo1234", help="Demo login password.")
    parser.add_argument(
        "--tenant-name", default="示範美髮沙龍", dest="tenant_name",
        help="Demo tenant name.",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="If demo tenant exists, refresh its demo rows in place (idempotent either way).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    session_factory: sessionmaker = SessionLocal,
    stdout: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    # standalone 入口：對空 DB 先建表（idempotent）。測試走注入 session 不經此路徑。
    if session_factory is SessionLocal:
        init_db()
    result = run(
        session_factory=session_factory,
        email=args.email,
        password=args.password,
        tenant_name=args.tenant_name,
        reset=args.reset,
    )
    write_report(result, out=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

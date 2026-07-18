"""R7-C4 — console JSON API:員工抽成/薪資結算(金錢狀態機)。"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from saas_mvp.app import create_app
from saas_mvp.auth.security import create_access_token
from saas_mvp.db import Base, get_db
from saas_mvp.models.product import Product
from saas_mvp.models.staff import Staff
from saas_mvp.models.user import User
from saas_mvp.services import commissions as commissions_svc
from saas_mvp.services import features as features_svc
from saas_mvp.services import pos as pos_svc

_TODAY = datetime.datetime.now(datetime.timezone.utc).date()
_PS = (_TODAY - datetime.timedelta(days=1)).isoformat()
_PE = (_TODAY + datetime.timedelta(days=1)).isoformat()


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


def _register(client: TestClient, prefix: str = "com") -> tuple[int, dict[str, str]]:
    unique = uuid.uuid4().hex[:8]
    r = client.post(
        "/auth/register",
        json={
            "email": f"{prefix}-{unique}@example.com",
            "password": "safe-password-123",
            "tenant_name": f"{prefix}-{unique}",
        },
    )
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ctx = client.get("/api/v1/context", headers=headers).json()
    return ctx["tenant"]["id"], headers


def _set_feature(session_factory, tenant_id: int, enabled: bool = True) -> None:
    db = session_factory()
    try:
        features_svc.set_enabled(
            db,
            tenant_id,
            features_svc.STAFF_COMMISSIONS,
            enabled,
            actor_user_id=None,
            source="admin",
        )
        db.commit()
    finally:
        db.close()


def _seed_staff_and_earning(session_factory, tenant_id: int) -> int:
    """員工 + 50% 抽成規則 + POS 已付訂單 → 產生一筆 500 元未結算抽成。"""
    db = session_factory()
    try:
        staff = Staff(tenant_id=tenant_id, name="Amy")
        product = Product(
            tenant_id=tenant_id, name="剪髮", price_cents=100000, stock=99
        )
        db.add_all([staff, product])
        db.flush()
        commissions_svc.save_rule(
            db,
            tenant_id=tenant_id,
            staff_id=staff.id,
            item_type="product",
            method="percent",
            value=5000,
            calculation_basis="net",
            effective_from=datetime.date(2020, 1, 1),
            actor_user_id=1,
        )
        pos_svc.checkout(
            db,
            tenant_id=tenant_id,
            customer_id=None,
            items=[{"product_id": product.id, "qty": 1}],
            staff_id=staff.id,
            payment_method="cash",
            mark_paid=True,
        )
        db.commit()
        return staff.id
    finally:
        db.close()


def _staff_headers(session_factory, tenant_id: int) -> dict[str, str]:
    db = session_factory()
    try:
        user = User(
            email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            tenant_id=tenant_id,
            role="staff",
        )
        db.add(user)
        db.commit()
        token = create_access_token(user.id, tenant_id)
    finally:
        db.close()
    return {"Authorization": f"Bearer {token}"}


def _mk_run(client, headers) -> dict:
    r = client.post(
        "/api/v1/commissions/pay-runs",
        json={"period_start": _PS, "period_end": _PE},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestCommissionsConsole:
    def test_feature_gate_403(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid, False)
        assert (
            client.get("/api/v1/commissions/overview", headers=headers).status_code
            == 403
        )

    def test_staff_role_403_even_readonly(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        staff = _staff_headers(sf, tid)
        # 薪資資料連唯讀都限 owner(比照 /ui 頁掛 require_ui_owner)
        assert (
            client.get("/api/v1/commissions/overview", headers=staff).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/commissions/pay-runs",
                json={"period_start": _PS, "period_end": _PE},
                headers=staff,
            ).status_code
            == 403
        )

    def test_rule_and_goal_create(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        staff_id = _seed_staff_and_earning(sf, tid)
        r = client.post(
            "/api/v1/commissions/rules",
            json={
                "staff_id": staff_id,
                "item_type": "service",
                "method": "percent",
                "value": "10",
                "effective_from": "2031-01-01",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        # percent 10% → 基點 1000(種子另有 product 50% 規則)
        ov = client.get("/api/v1/commissions/overview", headers=headers).json()
        service_rule = next(x for x in ov["rules"] if x["item_type"] == "service")
        assert service_rule["value"] == 1000
        assert ov["staff"] == [{"id": staff_id, "name": "Amy"}]
        # 目標 + 進度列
        g = client.post(
            "/api/v1/commissions/goals",
            json={
                "staff_id": staff_id,
                "target_twd": "50000",
                "effective_from": "2020-01-01",
            },
            headers=headers,
        )
        assert g.status_code == 201, g.text
        ov2 = client.get("/api/v1/commissions/overview", headers=headers).json()
        assert ov2["goals"][0]["target_cents"] == 5000000
        # 錯誤:非法 item_type / 壞金額
        assert (
            client.post(
                "/api/v1/commissions/rules",
                json={
                    "staff_id": staff_id,
                    "item_type": "bogus",
                    "method": "percent",
                    "value": "10",
                    "effective_from": "2031-01-01",
                },
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/commissions/goals",
                json={
                    "staff_id": staff_id,
                    "target_twd": "not-money",
                    "effective_from": "2031-01-01",
                },
                headers=headers,
            ).status_code
            == 422
        )

    def test_tiered_rule_create(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        staff_id = _seed_staff_and_earning(sf, tid)
        r = client.post(
            "/api/v1/commissions/tiered-rules",
            json={
                "staff_id": staff_id,
                "item_type": "service",
                "method": "percent",
                "sales_period": "monthly",
                "effective_from": "2031-01-01",
                "tiers": [
                    {"threshold_twd": "0", "value": "5"},
                    {"threshold_twd": "100000", "value": "8"},
                ],
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        ov = client.get("/api/v1/commissions/overview", headers=headers).json()
        rule = next(x for x in ov["rules"] if x["structure"] == "tiered")
        assert [t["value"] for t in rule["tiers"]] == [500, 800]

    def test_pay_run_lifecycle(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        staff_id = _seed_staff_and_earning(sf, tid)
        detail = _mk_run(client, headers)
        run_id = detail["run"]["id"]
        assert detail["run"]["status"] == "draft"
        assert detail["run"]["total_cents"] == 50000
        assert detail["items"][0]["staff_name"] == "Amy"
        # 空期間再建 → 422(沒有未結算明細)
        assert (
            client.post(
                "/api/v1/commissions/pay-runs",
                json={"period_start": _PS, "period_end": _PE},
                headers=headers,
            ).status_code
            == 422
        )
        # 調整 −100 → 總額 400
        r = client.post(
            f"/api/v1/commissions/pay-runs/{run_id}/adjust",
            json={"staff_id": staff_id, "adjustment_twd": "-100", "note": "借支"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["run"]["total_cents"] == 40000
        assert r.json()["items"][0]["adjustment_cents"] == -10000
        # 未確認前不可標付款 → 409
        assert (
            client.post(
                f"/api/v1/commissions/pay-runs/{run_id}/paid",
                json={},
                headers=headers,
            ).status_code
            == 409
        )
        # 確認
        r2 = client.post(
            f"/api/v1/commissions/pay-runs/{run_id}/finalize",
            json={},
            headers=headers,
        )
        assert r2.status_code == 200
        assert r2.json()["run"]["status"] == "finalized"
        # 確認後不可再調整 → 409(狀態衝突,與轉移端點一致)
        assert (
            client.post(
                f"/api/v1/commissions/pay-runs/{run_id}/adjust",
                json={"staff_id": staff_id, "adjustment_twd": "0"},
                headers=headers,
            ).status_code
            == 409
        )
        # 確認後不可刪 → 409
        assert (
            client.post(
                f"/api/v1/commissions/pay-runs/{run_id}/delete",
                json={},
                headers=headers,
            ).status_code
            == 409
        )
        # 標記付款
        r3 = client.post(
            f"/api/v1/commissions/pay-runs/{run_id}/paid", json={}, headers=headers
        )
        assert r3.status_code == 200
        assert r3.json()["run"]["status"] == "paid"
        # 重複付款 → 409
        assert (
            client.post(
                f"/api/v1/commissions/pay-runs/{run_id}/paid",
                json={},
                headers=headers,
            ).status_code
            == 409
        )

    def test_draft_delete_releases_earnings(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        _seed_staff_and_earning(sf, tid)
        run_id = _mk_run(client, headers)["run"]["id"]
        r = client.post(
            f"/api/v1/commissions/pay-runs/{run_id}/delete",
            json={},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 明細釋回 → 同期間可再建
        assert _mk_run(client, headers)["run"]["total_cents"] == 50000

    def test_draft_delete_with_reversal_pair_no_negative_run(self, v1_client):
        """對抗審查發現:draft 內的沖銷配對若被拆開釋回,重建結算單會
        變成 -500(員工被倒扣從未領過的錢)。修正後配對一併消滅。"""
        from saas_mvp.models.commission import CommissionEarning
        from saas_mvp.models.order import Order

        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        _seed_staff_and_earning(sf, tid)
        run_id = _mk_run(client, headers)["run"]["id"]
        # 訂單取消 → reverse_order 把 -50000 沖銷掛進同一張 draft
        db = sf()
        try:
            order = db.query(Order).filter(Order.tenant_id == tid).one()
            commissions_svc.reverse_order(db, order=order)
            db.commit()
        finally:
            db.close()
        detail = client.get(
            f"/api/v1/commissions/pay-runs/{run_id}", headers=headers
        ).json()
        assert detail["run"]["total_cents"] == 0  # 配對淨零
        # 刪除草稿:配對不得拆開釋回
        assert (
            client.post(
                f"/api/v1/commissions/pay-runs/{run_id}/delete",
                json={},
                headers=headers,
            ).status_code
            == 200
        )
        # 池中不得殘留裸沖銷;同期間重建 → 無未結算明細 → 422,而非 -50000
        r = client.post(
            "/api/v1/commissions/pay-runs",
            json={"period_start": _PS, "period_end": _PE},
            headers=headers,
        )
        assert r.status_code == 422, r.text
        db = sf()
        try:
            leftovers = (
                db.query(CommissionEarning)
                .filter(
                    CommissionEarning.tenant_id == tid,
                    CommissionEarning.reversal_of_id.is_not(None),
                )
                .count()
            )
            assert leftovers == 0
        finally:
            db.close()

    def test_huge_number_maps_422_not_500(self, v1_client):
        """對抗審查發現:>28 位數令 Decimal.quantize 拋 InvalidOperation
        → 500。修正後映射 422。"""
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        staff_id = _seed_staff_and_earning(sf, tid)
        assert (
            client.post(
                "/api/v1/commissions/rules",
                json={
                    "staff_id": staff_id,
                    "item_type": "service",
                    "method": "percent",
                    "value": "1e30",
                    "effective_from": "2020-01-01",
                },
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/commissions/goals",
                json={
                    "staff_id": staff_id,
                    "target_twd": "9" * 30,
                    "effective_from": "2020-01-01",
                },
                headers=headers,
            ).status_code
            == 422
        )

    def test_csv_exports(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        _seed_staff_and_earning(sf, tid)
        run_id = _mk_run(client, headers)["run"]["id"]
        r = client.get(
            f"/api/v1/commissions/pay-runs/{run_id}/export.csv", headers=headers
        )
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "Amy" in r.text
        r2 = client.get(
            "/api/v1/commissions/activity.csv",
            params={"period_start": _PS, "period_end": _PE},
            headers=headers,
        )
        assert r2.status_code == 200
        assert "剪髮" in r2.text

    def test_tenant_isolation_404(self, v1_client):
        client, sf = v1_client
        tid, headers = _register(client)
        _set_feature(sf, tid)
        _seed_staff_and_earning(sf, tid)
        run_id = _mk_run(client, headers)["run"]["id"]
        tid2, headers2 = _register(client, prefix="other")
        _set_feature(sf, tid2)
        assert (
            client.get(
                f"/api/v1/commissions/pay-runs/{run_id}", headers=headers2
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/v1/commissions/pay-runs/{run_id}/export.csv",
                headers=headers2,
            ).status_code
            == 404
        )
        # 轉移類:他租戶 run → service 找不到 → 409/404 皆不可成功
        assert client.post(
            f"/api/v1/commissions/pay-runs/{run_id}/finalize",
            json={},
            headers=headers2,
        ).status_code in (404, 409)

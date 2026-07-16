"""Google Calendar 單向寫入 + 可靠 outbox 重試。

* ``StubGcalClient``:記憶體 events dict(測試斷言)；平台後台與環境備援皆未
  設定 Google OAuth 時，factory 走 Stub 單例。
* ``HttpGcalClient``:refresh_token → access_token → Calendar API v3
  (stdlib urllib,零新相依)。
* 預約交易先寫 ``GcalSyncJob``，即時同步失敗時由 scheduler 指數退避補送。
* 每筆預約只保留最新意圖（upsert/delete），快速改期或取消不會送出過期狀態。
* 新增事件使用可重現的 event id；API 回應遺失後重試也不會建立重複事件。
* 整層失敗永不影響預約主流程，錯誤同時記在憑證與同步工作供後台診斷。
"""

from __future__ import annotations

import datetime
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saas_mvp.config import settings

_log = logging.getLogger(__name__)


class GcalError(Exception):
    pass


class GcalNotFound(GcalError):
    pass


class GcalConflict(GcalError):
    pass


class GcalClient:
    def insert_event(self, *, calendar_id: str, refresh_token: str, event: dict) -> str:
        raise NotImplementedError

    def patch_event(self, *, calendar_id: str, refresh_token: str,
                    event_id: str, event: dict) -> None:
        raise NotImplementedError

    def delete_event(self, *, calendar_id: str, refresh_token: str,
                     event_id: str) -> None:
        raise NotImplementedError

    def get_event(self, *, calendar_id: str, refresh_token: str,
                  event_id: str) -> dict | None:
        """回單一事件(含 status/start);不存在回 None(漂移偵測用,R4-B3)。"""
        raise NotImplementedError


class StubGcalClient(GcalClient):
    """離線 stub:events[event_id] = event。"""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self._seq = 0

    def insert_event(self, *, calendar_id, refresh_token, event) -> str:
        event_id = str(event.get("id") or "")
        if not event_id:
            self._seq += 1
            event_id = f"stub-evt-{self._seq}"
        self.events[event_id] = dict(event)
        return event_id

    def patch_event(self, *, calendar_id, refresh_token, event_id, event) -> None:
        if event_id in self.events:
            self.events[event_id].update(event)

    def delete_event(self, *, calendar_id, refresh_token, event_id) -> None:
        self.events.pop(event_id, None)

    def get_event(self, *, calendar_id, refresh_token, event_id) -> dict | None:
        return self.events.get(event_id)


class UnconfiguredGcalClient(GcalClient):
    """正式環境缺平台 OAuth 憑證時明確失敗，避免 Stub 假同步成功。"""

    @staticmethod
    def _fail() -> None:
        raise GcalError("平台 Google OAuth 尚未設定，請聯絡平台管理員")

    def insert_event(self, **kwargs) -> str:
        self._fail()

    def patch_event(self, **kwargs) -> None:
        self._fail()

    def delete_event(self, **kwargs) -> None:
        self._fail()

    def get_event(self, **kwargs) -> dict | None:
        self._fail()


class HttpGcalClient(GcalClient):
    """真實 Calendar API v3;refresh token 換 access token 後呼叫。"""

    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self._client_id = client_id or settings.google_oauth_client_id
        self._client_secret = client_secret or settings.google_oauth_client_secret

    def _access_token(self, refresh_token: str) -> str:
        body = urllib.parse.urlencode({
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise GcalError(f"token refresh failed: {exc}") from exc
        token = data.get("access_token")
        if not token:
            raise GcalError(f"token refresh rejected: {data.get('error')}")
        return token

    def _call(self, method: str, url: str, refresh_token: str,
              payload: dict | None = None) -> dict:
        token = self._access_token(refresh_token)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise GcalNotFound("calendar event not found") from exc
            if exc.code == 409:
                raise GcalConflict("calendar event already exists") from exc
            raise GcalError(f"calendar api failed: HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise GcalError(f"calendar api failed: {exc}") from exc

    def insert_event(self, *, calendar_id, refresh_token, event) -> str:
        cal = urllib.parse.quote(calendar_id)
        try:
            data = self._call(
                "POST",
                f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events",
                refresh_token, event,
            )
        except GcalConflict:
            # 事件 id 由預約 id 決定；409 表示先前請求其實已成功，只是回應遺失。
            return str(event.get("id") or "")
        return str(data.get("id") or "")

    def patch_event(self, *, calendar_id, refresh_token, event_id, event) -> None:
        cal = urllib.parse.quote(calendar_id)
        self._call(
            "PATCH",
            f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events/{event_id}",
            refresh_token, event,
        )

    def delete_event(self, *, calendar_id, refresh_token, event_id) -> None:
        cal = urllib.parse.quote(calendar_id)
        try:
            self._call(
                "DELETE",
                f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events/{event_id}",
                refresh_token, None,
            )
        except GcalNotFound:
            return  # 刪除本身具冪等性：遠端已不存在即視為完成。

    def get_event(self, *, calendar_id, refresh_token, event_id) -> dict | None:
        cal = urllib.parse.quote(calendar_id)
        try:
            return self._call(
                "GET",
                f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events/{event_id}",
                refresh_token, None,
            )
        except GcalNotFound:
            return None  # 事件已在 Google 端被刪 → 漂移


_stub_singleton = StubGcalClient()


def get_gcal_client(db: Session | None = None) -> GcalClient:
    """資料庫後台設定優先；未設定時使用離線 Stub。"""
    from saas_mvp.services.platform_oauth_config import effective_google_credentials

    credentials = effective_google_credentials(db, settings)
    if credentials:
        return HttpGcalClient(client_id=credentials[0], client_secret=credentials[1])
    if settings.env not in ("dev", "test"):
        return UnconfiguredGcalClient()
    return _stub_singleton


def _event_payload(resv, slot) -> dict:
    start = slot.slot_start
    end = slot.slot_end or (start + datetime.timedelta(hours=1))

    def _iso(dt):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.isoformat()

    return {
        "summary": f"預約 #{resv.id}({resv.party_size} 位)",
        "start": {"dateTime": _iso(start)},
        "end": {"dateTime": _iso(end)},
    }


MAX_ATTEMPTS = 8
_RETRY_SECONDS = (60, 300, 900, 3600, 10800, 21600, 43200)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _deterministic_event_id(resv) -> str:
    """Google event id 僅允許 base32hex 字元（0-9、a-v）。"""
    return f"saas{resv.tenant_id:x}e{resv.id:x}"


def enqueue_reservation_sync(db: Session, resv, kind: str, *, now=None):
    """在預約主交易內保存最新同步意圖；租戶未連結 Google 時 no-op。"""
    from saas_mvp.models.gcal_sync_job import (
        GCAL_ACTION_DELETE,
        GCAL_ACTION_UPSERT,
        GCAL_SYNC_PENDING,
        GcalSyncJob,
    )
    from saas_mvp.models.tenant_gcal_credential import TenantGcalCredential

    cred_id = db.execute(
        select(TenantGcalCredential.id).where(
            TenantGcalCredential.tenant_id == resv.tenant_id
        )
    ).scalar_one_or_none()
    if cred_id is None:
        return None
    effective_now = now or _utcnow()
    row = db.execute(
        select(GcalSyncJob).where(GcalSyncJob.reservation_id == resv.id)
    ).scalar_one_or_none()
    if row is None:
        row = GcalSyncJob(tenant_id=resv.tenant_id, reservation_id=resv.id)
        db.add(row)
    row.action = GCAL_ACTION_DELETE if kind in {"cancel", "delete"} else GCAL_ACTION_UPSERT
    row.status = GCAL_SYNC_PENDING
    row.attempt_count = 0
    # 即時嘗試與排程器保留一分鐘隔離窗，避免兩邊同時取得工作。
    row.next_attempt_at = effective_now + datetime.timedelta(minutes=1)
    row.last_error = None
    row.synced_at = None
    row.updated_at = effective_now
    db.flush()
    return row


def _mark_failure(db: Session, row, cred, exc: Exception, now: datetime.datetime) -> str:
    from saas_mvp.models.gcal_sync_job import GCAL_SYNC_FAILED, GCAL_SYNC_PENDING
    from saas_mvp.models.tenant_gcal_credential import GCAL_ERROR

    row.attempt_count = (row.attempt_count or 0) + 1
    row.last_error = str(exc)[:255] if isinstance(exc, GcalError) else type(exc).__name__
    row.updated_at = now
    cred.status = GCAL_ERROR
    cred.last_error = row.last_error
    if row.attempt_count >= MAX_ATTEMPTS:
        row.status = GCAL_SYNC_FAILED
        row.next_attempt_at = None
    else:
        row.status = GCAL_SYNC_PENDING
        delay = _RETRY_SECONDS[min(row.attempt_count - 1, len(_RETRY_SECONDS) - 1)]
        row.next_attempt_at = now + datetime.timedelta(seconds=delay)
    from saas_mvp.obs.alerts import capture_alert

    capture_alert(f"gcal sync failed tenant={row.tenant_id}: {row.last_error}")
    return row.status


def attempt_sync(db: Session, row, *, client: GcalClient | None = None, now=None) -> str:
    """執行單筆同步並更新 outbox 狀態；攔截所有錯誤，不影響預約。"""
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.gcal_sync_job import (
        GCAL_ACTION_DELETE,
        GCAL_SYNC_CANCELED,
        GCAL_SYNC_SYNCED,
    )
    from saas_mvp.models.reservation import RESERVATION_CANCELLED, Reservation
    from saas_mvp.models.tenant_gcal_credential import (
        GCAL_CONNECTED,
        TenantGcalCredential,
    )

    effective_now = now or _utcnow()
    cred = db.execute(
        select(TenantGcalCredential).where(TenantGcalCredential.tenant_id == row.tenant_id)
    ).scalar_one_or_none()
    resv = db.get(Reservation, row.reservation_id)
    if cred is None or resv is None:
        row.status = GCAL_SYNC_CANCELED
        row.next_attempt_at = None
        row.updated_at = effective_now
        return row.status

    effective = client or get_gcal_client(db)
    try:
        should_delete = row.action == GCAL_ACTION_DELETE or resv.status == RESERVATION_CANCELLED
        if should_delete:
            event_id = resv.gcal_event_id or _deterministic_event_id(resv)
            effective.delete_event(
                calendar_id=cred.calendar_id,
                refresh_token=cred.refresh_token,
                event_id=event_id,
            )
            resv.gcal_event_id = None
        else:
            slot = db.get(BookingSlot, resv.slot_id)
            if slot is None:
                raise GcalError("booking slot no longer exists")
            payload = _event_payload(resv, slot)
            insert_payload = {"id": _deterministic_event_id(resv), **payload}
            if resv.gcal_event_id:
                try:
                    effective.patch_event(
                        calendar_id=cred.calendar_id,
                        refresh_token=cred.refresh_token,
                        event_id=resv.gcal_event_id,
                        event=payload,
                    )
                except GcalNotFound:
                    resv.gcal_event_id = effective.insert_event(
                        calendar_id=cred.calendar_id,
                        refresh_token=cred.refresh_token,
                        event=insert_payload,
                    ) or None
            else:
                resv.gcal_event_id = effective.insert_event(
                    calendar_id=cred.calendar_id,
                    refresh_token=cred.refresh_token,
                    event=insert_payload,
                ) or None
                if not resv.gcal_event_id:
                    raise GcalError("calendar api returned no event id")
    except Exception as exc:  # noqa: BLE001 — outbox 必須攔截並排程重試
        return _mark_failure(db, row, cred, exc, effective_now)

    row.status = GCAL_SYNC_SYNCED
    row.attempt_count = (row.attempt_count or 0) + 1
    row.next_attempt_at = None
    row.last_error = None
    row.synced_at = effective_now
    row.updated_at = effective_now
    cred.status = GCAL_CONNECTED
    cred.last_error = None
    # R4-B3:重新同步成功即清除既有漂移旗標(店家改動已被本系統覆寫回)。
    if resv.gcal_drift_detected_at is not None:
        resv.gcal_drift_detected_at = None
        resv.gcal_drift_note = None
    return row.status


def attempt_reservation_sync(db: Session, reservation_id: int, *, client=None, now=None):
    from saas_mvp.models.gcal_sync_job import GcalSyncJob

    try:
        row = db.execute(
            select(GcalSyncJob).where(GcalSyncJob.reservation_id == reservation_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return attempt_sync(db, row, client=client, now=now)
    except Exception:  # noqa: BLE001 — 預約已提交，同步層不得讓回應失敗
        db.rollback()
        _log.warning("gcal immediate sync unexpected failure", exc_info=True)
        return None


def sync_reservation(db: Session, resv, kind: str, *, client: GcalClient | None = None) -> None:
    """向後相容的即時同步入口；新流程會先在主交易呼叫 enqueue。"""
    try:
        row = enqueue_reservation_sync(db, resv, kind)
        if row is not None:
            attempt_sync(db, row, client=client)
            db.flush()
    except Exception:  # noqa: BLE001 — 同步絕不影響預約主流程
        _log.warning("gcal sync unexpected failure", exc_info=True)


def due_ids(db: Session, *, now: datetime.datetime, limit: int = 100) -> list[int]:
    from saas_mvp.models.gcal_sync_job import GCAL_SYNC_PENDING, GcalSyncJob

    return list(db.execute(
        select(GcalSyncJob.id)
        .where(GcalSyncJob.status == GCAL_SYNC_PENDING, GcalSyncJob.next_attempt_at <= now)
        .order_by(GcalSyncJob.next_attempt_at, GcalSyncJob.id)
        .limit(limit)
    ).scalars())


def summary(db: Session, tenant_id: int) -> dict[str, int]:
    from saas_mvp.models.gcal_sync_job import (
        GCAL_SYNC_FAILED,
        GCAL_SYNC_PENDING,
        GCAL_SYNC_SYNCED,
        GcalSyncJob,
    )

    counts = dict(db.execute(
        select(GcalSyncJob.status, func.count(GcalSyncJob.id))
        .where(GcalSyncJob.tenant_id == tenant_id)
        .group_by(GcalSyncJob.status)
    ).all())
    return {key: int(counts.get(key, 0)) for key in (
        GCAL_SYNC_PENDING, GCAL_SYNC_SYNCED, GCAL_SYNC_FAILED
    )}


def retry_failed(db: Session, tenant_id: int, *, now=None) -> int:
    from saas_mvp.models.gcal_sync_job import (
        GCAL_SYNC_FAILED,
        GCAL_SYNC_PENDING,
        GcalSyncJob,
    )

    db.flush()
    rows = list(db.execute(
        select(GcalSyncJob).where(
            GcalSyncJob.tenant_id == tenant_id,
            GcalSyncJob.status == GCAL_SYNC_FAILED,
        )
    ).scalars())
    effective_now = now or _utcnow()
    for row in rows:
        row.status = GCAL_SYNC_PENDING
        row.attempt_count = 0
        row.next_attempt_at = effective_now
        row.last_error = None
        row.updated_at = effective_now
    return len(rows)


# ── 漂移偵測(R4-B3):輪詢比對 Google 端是否被改/刪,通知店家,不改預約 ─────────

def _drift_note(event: dict | None, resv, slot) -> str | None:
    """比對 Google 事件與本地預約,回漂移說明;一致回 None。event=None 表已刪。"""
    if event is None:
        return "Google 日曆事件已被刪除"
    status = str(event.get("status") or "").lower()
    if status == "cancelled":
        return "Google 日曆事件已標記取消"
    # start 時間比對:Google 回 RFC3339,本地 slot_start 可能 naive。
    start = (event.get("start") or {}).get("dateTime")
    if start and slot is not None:
        try:
            google_dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
            local = slot.slot_start
            g = google_dt.replace(tzinfo=None)
            loc = local.replace(tzinfo=None) if local.tzinfo else local
            if abs((g - loc).total_seconds()) > 60:
                return f"Google 日曆時間被更改為 {g.strftime('%Y-%m-%d %H:%M')}"
        except (ValueError, AttributeError):
            pass
    return None


def check_drift_for_tenant(
    db: Session,
    tenant_id: int,
    *,
    client: GcalClient | None = None,
    mailer=None,
    now: datetime.datetime | None = None,
    apply: bool = True,
) -> dict:
    """輪詢該租戶已同步的未來預約,偵測 Google 端漂移(改/刪/取消)。

    命中且尚未標記過 → 設 drift 欄 + best-effort email 通知租戶使用者(每筆只寄
    一次)。**絕不改動預約狀態**。re-sync 一致的預約清空 drift 旗標。回計數。

    apply=False 為 dry-run:僅計數,不寫 drift 欄、不通知、不 commit(給 cron 預覽)。
    """
    from saas_mvp.models.booking_slot import BookingSlot
    from saas_mvp.models.reservation import RESERVATION_CONFIRMED, Reservation
    from saas_mvp.models.tenant_gcal_credential import (
        GCAL_CONNECTED,
        TenantGcalCredential,
    )

    effective_now = now or _utcnow()
    cred = db.execute(
        select(TenantGcalCredential).where(
            TenantGcalCredential.tenant_id == tenant_id,
            TenantGcalCredential.status == GCAL_CONNECTED,
        )
    ).scalar_one_or_none()
    if cred is None:
        return {"checked": 0, "drift": 0, "cleared": 0}

    effective = client or get_gcal_client(db)
    # 只看「未來、已同步(有 event_id)、confirmed」的預約。
    rows = db.execute(
        select(Reservation, BookingSlot)
        .join(BookingSlot, Reservation.slot_id == BookingSlot.id)
        .where(
            Reservation.tenant_id == tenant_id,
            Reservation.status == RESERVATION_CONFIRMED,
            Reservation.gcal_event_id.is_not(None),
            BookingSlot.slot_start >= effective_now.replace(tzinfo=None),
        )
    ).all()

    summary = {"checked": 0, "drift": 0, "cleared": 0}
    for resv, slot in rows:
        summary["checked"] += 1
        try:
            event = effective.get_event(
                calendar_id=cred.calendar_id,
                refresh_token=cred.refresh_token,
                event_id=resv.gcal_event_id,
            )
        except Exception:  # noqa: BLE001 — 單筆查詢失敗不中斷整輪
            _log.warning("gcal drift check failed resv=%s", resv.id, exc_info=True)
            continue
        note = _drift_note(event, resv, slot)
        if note is None:
            # 一致:若先前標記過漂移,清空(已 re-sync 或店家改回)。
            if resv.gcal_drift_detected_at is not None:
                summary["cleared"] += 1
                if apply:
                    resv.gcal_drift_detected_at = None
                    resv.gcal_drift_note = None
            continue
        if resv.gcal_drift_detected_at is not None:
            continue  # 已標記過,不重複通知
        summary["drift"] += 1
        if apply:
            resv.gcal_drift_detected_at = effective_now
            resv.gcal_drift_note = note[:255]
            _notify_owner_drift(db, tenant_id, resv, note, mailer)

    if apply:
        cred.last_drift_check_at = effective_now
        db.commit()
    else:
        db.rollback()
    return summary


def _notify_owner_drift(db: Session, tenant_id: int, resv, note: str, mailer) -> None:
    """best-effort email 通知租戶 owner/staff:某預約的 Google 事件漂移。"""
    try:
        from saas_mvp.models.user import User
        from saas_mvp.services import email_delivery as email_svc
        from saas_mvp.services.mailer import get_mailer

        m = mailer or get_mailer(db)
        users = db.execute(
            select(User).where(User.tenant_id == tenant_id)
        ).scalars().all()
        subject = f"【日曆同步提醒】預約 #{resv.id} 的 Google 日曆事件有異動"
        body = (
            f"預約 #{resv.id} 在 Google 日曆的事件偵測到異動:{note}。\n\n"
            "系統採單向同步,不會自動改動本系統的預約;請確認是否需在後台手動處理。"
        )
        for u in users:
            if not u.email:
                continue
            email_svc.deliver_or_queue(
                db, m, user_id=u.id, category="gcal_drift",
                recipient=u.email, subject=subject, body=body,
            )
    except Exception:  # noqa: BLE001 — 通知永不影響偵測
        _log.warning("gcal drift notify failed resv=%s", resv.id, exc_info=True)

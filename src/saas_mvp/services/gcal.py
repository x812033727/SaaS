"""Google Calendar 單向寫入 + 可靠 outbox 重試。

* ``StubGcalClient``:記憶體 events dict(測試斷言);``google_oauth_client_id``
  空時 factory 走 Stub 單例(mailer 型態)。
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


class HttpGcalClient(GcalClient):
    """真實 Calendar API v3;refresh token 換 access token 後呼叫。"""

    def _access_token(self, refresh_token: str) -> str:
        body = urllib.parse.urlencode({
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
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


_stub_singleton = StubGcalClient()


def get_gcal_client() -> GcalClient:
    """factory:google_oauth_client_id 空 → Stub 單例(離線/未設定)。"""
    if settings.google_oauth_client_id:
        return HttpGcalClient()
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

    effective = client or get_gcal_client()
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

"""Google Calendar 單向寫入（E1 Step B）— Stub/Http 雙模式,失敗永不阻擋主流程。

* ``StubGcalClient``:記憶體 events dict(測試斷言);``google_oauth_client_id``
  空時 factory 走 Stub 單例(mailer 型態)。
* ``HttpGcalClient``:refresh_token → access_token → Calendar API v3
  (stdlib urllib,零新相依)。
* ``sync_reservation(db, resv, kind)``:book/reschedule/cancel 掛點各一行;
  無憑證 no-op;**整層 try/except 永不拋**,失敗 capture_alert + 記
  credentials.last_error。失敗事件不自動補償(列 KNOWN_LIMITATIONS)。
"""

from __future__ import annotations

import datetime
import json
import logging
import urllib.parse
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.config import settings

_log = logging.getLogger(__name__)


class GcalError(Exception):
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
        except Exception as exc:  # noqa: BLE001
            raise GcalError(f"calendar api failed: {exc}") from exc

    def insert_event(self, *, calendar_id, refresh_token, event) -> str:
        cal = urllib.parse.quote(calendar_id)
        data = self._call(
            "POST",
            f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events",
            refresh_token, event,
        )
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
        self._call(
            "DELETE",
            f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events/{event_id}",
            refresh_token, None,
        )


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


def sync_reservation(db: Session, resv, kind: str, *, client: GcalClient | None = None) -> None:
    """book/reschedule/cancel 同步(best-effort,永不拋)。"""
    try:
        from saas_mvp.models.booking_slot import BookingSlot
        from saas_mvp.models.tenant_gcal_credential import (
            GCAL_CONNECTED,
            GCAL_ERROR,
            TenantGcalCredential,
        )

        cred = db.execute(
            select(TenantGcalCredential).where(
                TenantGcalCredential.tenant_id == resv.tenant_id
            )
        ).scalar_one_or_none()
        if cred is None:
            return  # 未連結:no-op

        effective = client or get_gcal_client()
        slot = db.get(BookingSlot, resv.slot_id)
        if slot is None:
            return

        try:
            did_call = False
            if kind == "create":
                event_id = effective.insert_event(
                    calendar_id=cred.calendar_id,
                    refresh_token=cred.refresh_token,
                    event=_event_payload(resv, slot),
                )
                resv.gcal_event_id = event_id or None
                did_call = True
            elif kind == "reschedule" and resv.gcal_event_id:
                effective.patch_event(
                    calendar_id=cred.calendar_id,
                    refresh_token=cred.refresh_token,
                    event_id=resv.gcal_event_id,
                    event=_event_payload(resv, slot),
                )
                did_call = True
            elif kind == "cancel" and resv.gcal_event_id:
                effective.delete_event(
                    calendar_id=cred.calendar_id,
                    refresh_token=cred.refresh_token,
                    event_id=resv.gcal_event_id,
                )
                did_call = True
            # 僅在確實打了 API 才把狀態標回 connected/清 last_error;
            # reschedule/cancel 遇 gcal_event_id 為空是 no-op,不得抹除先前的錯誤狀態。
            if did_call:
                cred.status = GCAL_CONNECTED
                cred.last_error = None
        except GcalError as exc:
            cred.status = GCAL_ERROR
            cred.last_error = str(exc)[:255]
            from saas_mvp.obs.alerts import capture_alert

            capture_alert(f"gcal sync failed tenant={resv.tenant_id}: {exc}")
    except Exception:  # noqa: BLE001 — 同步絕不影響預約主流程
        _log.warning("gcal sync unexpected failure", exc_info=True)

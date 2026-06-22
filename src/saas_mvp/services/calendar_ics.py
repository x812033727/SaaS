"""行事曆同步服務 — ICS（iCalendar）feed 產生 + Add-to-Google-Calendar 連結。

純標準函式庫（urllib.parse），無外部相依。

build_ics 由一組 event dict 組出合法 VCALENDAR/VEVENT 文字：
  * 正常用 METHOD:PUBLISH；任一 event status=='cancelled' 時改 METHOD:CANCEL
    並對該 event 標 STATUS:CANCELLED。
  * SEQUENCE 取自 event['sequence']（由 reservation.updated_at epoch 推導）。
  * DTSTAMP 取自傳入的 now 或 event['updated_at']。
  * 時間一律格式化為 UTC 的 YYYYMMDDTHHMMSSZ。

google_calendar_url 組出「加入 Google 行事曆」的 TEMPLATE 連結。
"""

from __future__ import annotations

import datetime
import secrets
from urllib.parse import urlencode


def ensure_ics_token(db, obj) -> str:
    """惰性產生並 commit 物件的 ics_token（若尚未有）；回傳 token。

    obj 可為 Tenant 或 Customer（皆有 ics_token 欄位）。token 即能力，
    使用 secrets.token_urlsafe(32) 產生不可猜測值。已有則直接回傳、不重產。
    """
    token = getattr(obj, "ics_token", None)
    if token:
        return token
    token = secrets.token_urlsafe(32)
    obj.ics_token = token
    db.commit()
    db.refresh(obj)
    return obj.ics_token


def _fmt_dt(dt: datetime.datetime) -> str:
    """格式化為 iCalendar UTC 時間戳 YYYYMMDDTHHMMSSZ。

    tz-aware 先轉 UTC；naive 視為 UTC。
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _escape_text(value: str) -> str:
    """iCalendar TEXT 值跳脫（RFC 5545 §3.3.11）。"""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    """RFC 5545 行折疊：>75 octet 的行折成多行（續行以單空格起首）。"""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for ch in line:
        ch_bytes = len(ch.encode("utf-8"))
        # 續行前綴佔 1 octet，故第二行起上限 74。
        limit = 75 if not chunks else 74
        if current_bytes + ch_bytes > limit:
            chunks.append(current)
            current = ch
            current_bytes = ch_bytes
        else:
            current += ch
            current_bytes += ch_bytes
    if current:
        chunks.append(current)
    return "\r\n ".join(chunks)


def build_ics(
    events: list[dict],
    *,
    now: datetime.datetime | None = None,
    prodid: str = "-//SaaS MVP//Booking//ZH-TW",
) -> str:
    """由 event dict 清單組出 VCALENDAR 文字。

    每個 event dict 欄位：
      * uid（str，必填）
      * summary（str，必填）
      * start（tz-aware datetime，必填）
      * end（datetime，選填；缺則用 start）
      * status（'confirmed'|'cancelled'，預設 confirmed）
      * sequence（int，預設 0）
      * location（str，選填）
      * description（str，選填）
      * updated_at（datetime，選填；無傳入 now 時作 DTSTAMP）
    """
    any_cancelled = any(
        (ev.get("status") or "confirmed") == "cancelled" for ev in events
    )
    method = "CANCEL" if any_cancelled else "PUBLISH"

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
    ]

    for ev in events:
        status = (ev.get("status") or "confirmed")
        start = ev["start"]
        end = ev.get("end") or start
        dtstamp = now or ev.get("updated_at") or start
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{_escape_text(str(ev['uid']))}")
        lines.append(f"DTSTAMP:{_fmt_dt(dtstamp)}")
        lines.append(f"DTSTART:{_fmt_dt(start)}")
        lines.append(f"DTEND:{_fmt_dt(end)}")
        lines.append(f"SEQUENCE:{int(ev.get('sequence') or 0)}")
        lines.append(f"SUMMARY:{_escape_text(str(ev.get('summary') or ''))}")
        if ev.get("location"):
            lines.append(f"LOCATION:{_escape_text(str(ev['location']))}")
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{_escape_text(str(ev['description']))}")
        if status == "cancelled":
            lines.append("STATUS:CANCELLED")
        else:
            lines.append("STATUS:CONFIRMED")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold_line(ln) for ln in lines) + "\r\n"


def google_calendar_url(
    *,
    title: str,
    start: datetime.datetime,
    end: datetime.datetime,
    details: str | None = None,
    location: str | None = None,
) -> str:
    """組出「加入 Google 行事曆」TEMPLATE 連結。

    格式：
      https://calendar.google.com/calendar/render?action=TEMPLATE
        &text=...&dates=<start>/<end>&details=...&location=...
    時間以 UTC YYYYMMDDTHHMMSSZ 表示。
    """
    params: dict[str, str] = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{_fmt_dt(start)}/{_fmt_dt(end)}",
    }
    if details is not None:
        params["details"] = details
    if location is not None:
        params["location"] = location
    return (
        "https://calendar.google.com/calendar/render?" + urlencode(params)
    )

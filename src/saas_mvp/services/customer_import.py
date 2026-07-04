"""顧客 CSV 批次匯入（店家自有名單搬移）。

格式（UTF-8,容忍 Excel BOM）:
    display_name(必填), phone, birthday(YYYY-MM-DD), note

策略:
  * 兩段式 all-or-nothing:先全檔解析驗證（收集錯誤:列號+原因）,
    有任何錯誤整批不寫;全數合法才單一交易寫入。避免半套匯入。
  * 重複判定:同租戶 + 正規化電話（去空白/`-`、`+886`→`0`）比對。
    預設 skip;update_existing=True 時只覆寫「非空」欄位。
    無 phone 的列一律新增、不去重。
  * 上限 _MAX_ROWS 列 / _MAX_BYTES,超過整檔拒絕。
  * 匯入的顧客 line_user_id=None（無 LINE 來源;推播路徑一律 guard None）。
"""

from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from saas_mvp.models.customer import Customer
from saas_mvp.services.tenants import tenant_query

_MAX_ROWS = 5000
_MAX_BYTES = 1_000_000  # 1MB
_MAX_ERRORS_REPORTED = 20

_REQUIRED_COLUMNS = {"display_name"}
_KNOWN_COLUMNS = {"display_name", "phone", "birthday", "note"}


class ImportError_(Exception):
    """整檔層級錯誤（格式/大小/欄位）。"""


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def normalize_phone(raw: str | None) -> str | None:
    """電話正規化供重複比對:去空白/連字號,+886 開頭轉 0。"""
    if not raw:
        return None
    phone = raw.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+886"):
        phone = "0" + phone[4:]
    return phone or None


def _parse_rows(content: bytes) -> list[dict]:
    """解碼 + DictReader;整檔層級錯誤拋 ImportError_。"""
    if len(content) > _MAX_BYTES:
        raise ImportError_(f"檔案超過大小上限（{_MAX_BYTES // 1000}KB）")
    try:
        text = content.decode("utf-8-sig")  # 容忍 Excel BOM
    except UnicodeDecodeError:
        raise ImportError_("檔案不是 UTF-8 編碼")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportError_("空檔案")
    fieldnames = {(f or "").strip() for f in reader.fieldnames}
    missing = _REQUIRED_COLUMNS - fieldnames
    if missing:
        raise ImportError_(
            f"缺少必要欄位: {', '.join(sorted(missing))}"
            f"（表頭需含 display_name,可選 phone/birthday/note）"
        )
    rows = list(reader)
    if len(rows) > _MAX_ROWS:
        raise ImportError_(f"超過 {_MAX_ROWS} 列上限（共 {len(rows)} 列）")
    return rows


def _validate(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """逐列驗證;回傳 (正規化列, 錯誤訊息清單)。"""
    parsed: list[dict] = []
    errors: list[str] = []
    for idx, row in enumerate(rows, start=2):  # 列號含表頭（第 1 列）
        name = (row.get("display_name") or "").strip()
        if not name:
            errors.append(f"第 {idx} 列: display_name 不可為空")
            continue
        if len(name) > 128:
            errors.append(f"第 {idx} 列: display_name 超過 128 字")
            continue
        phone = normalize_phone(row.get("phone"))
        if phone and len(phone) > 32:
            errors.append(f"第 {idx} 列: phone 超過 32 字")
            continue
        birthday = None
        raw_birthday = (row.get("birthday") or "").strip()
        if raw_birthday:
            try:
                birthday = datetime.date.fromisoformat(raw_birthday)
            except ValueError:
                errors.append(
                    f"第 {idx} 列: birthday 格式錯誤（需 YYYY-MM-DD）: {raw_birthday!r}"
                )
                continue
        note = (row.get("note") or "").strip()[:2048] or None
        parsed.append({
            "display_name": name,
            "phone": phone,
            "birthday": birthday,
            "note": note,
        })
    return parsed, errors


def import_customers(
    db: Session,
    *,
    tenant_id: int,
    content: bytes,
    update_existing: bool = False,
) -> ImportReport:
    """匯入顧客 CSV。errors 非空時保證 DB 零寫入。"""
    report = ImportReport()
    try:
        rows = _parse_rows(content)
    except ImportError_ as exc:
        report.errors.append(str(exc))
        return report

    parsed, errors = _validate(rows)
    if errors:
        report.errors = errors[:_MAX_ERRORS_REPORTED]
        if len(errors) > _MAX_ERRORS_REPORTED:
            report.errors.append(
                f"…另有 {len(errors) - _MAX_ERRORS_REPORTED} 筆錯誤未列出"
            )
        return report  # all-or-nothing:有錯整批不寫

    # 既有顧客電話 map（一次撈,匯入內重複 phone 也以先到者為準）
    existing_by_phone: dict[str, Customer] = {}
    for c in tenant_query(db, Customer, tenant_id).filter(
        Customer.phone.is_not(None)
    ):
        key = normalize_phone(c.phone)
        if key and key not in existing_by_phone:
            existing_by_phone[key] = c

    for row in parsed:
        phone = row["phone"]
        existing = existing_by_phone.get(phone) if phone else None
        if existing is not None:
            if update_existing:
                existing.display_name = row["display_name"]
                if row["birthday"] is not None:
                    existing.birthday = row["birthday"]
                if row["note"]:
                    existing.note = row["note"]
                report.updated += 1
            else:
                report.skipped += 1
            continue
        customer = Customer(
            tenant_id=tenant_id,
            line_user_id=None,  # 無 LINE 來源
            display_name=row["display_name"],
            phone=phone,
            birthday=row["birthday"],
            note=row["note"],
        )
        db.add(customer)
        if phone:
            existing_by_phone[phone] = customer  # 檔內重複 phone 去重
        report.created += 1

    db.commit()  # 單一交易,全數合法才落盤
    return report

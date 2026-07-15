"""店家自助電子發票資料：驗證、加密保存與安全顯示。"""

from __future__ import annotations

import dataclasses
import re

from sqlalchemy.orm import Session

from saas_mvp.models.tenant_invoice_profile import TenantInvoiceProfile

MODE_PERSONAL = "personal"
MODE_BUSINESS = "business"
MODE_DONATION = "donation"
VALID_MODES = frozenset({MODE_PERSONAL, MODE_BUSINESS, MODE_DONATION})

CARRIER_ECPAY = "ecpay"
CARRIER_MOBILE = "mobile"
CARRIER_CITIZEN = "citizen"
VALID_CARRIERS = frozenset({CARRIER_ECPAY, CARRIER_MOBILE, CARRIER_CITIZEN})

_MOBILE_RE = re.compile(r"^/[0-9A-Z+\-.]{7}$")
_CITIZEN_RE = re.compile(r"^[A-Z]{2}[0-9]{14}$")
_IDENTIFIER_RE = re.compile(r"^[0-9]{8}$")
_DONATION_RE = re.compile(r"^[0-9]{3,7}$")
_IDENTIFIER_WEIGHTS = (1, 2, 1, 2, 1, 2, 4, 1)


class InvoiceProfileError(ValueError):
    """發票資料無效。"""


@dataclasses.dataclass(frozen=True)
class InvoiceProfileData:
    mode: str = MODE_PERSONAL
    buyer_name: str = ""
    buyer_identifier: str = ""
    carrier_type: str = CARRIER_ECPAY
    carrier_number: str = ""
    donation_code: str = ""


def _valid_identifier(value: str) -> bool:
    """財政部新版統編檢核：各位加權拆位相加後可被 5 整除。"""
    if not _IDENTIFIER_RE.fullmatch(value):
        return False
    total = 0
    for digit, weight in zip(value, _IDENTIFIER_WEIGHTS, strict=True):
        product = int(digit) * weight
        total += product // 10 + product % 10
    return total % 5 == 0


def _row(db: Session, tenant_id: int) -> TenantInvoiceProfile | None:
    return (
        db.query(TenantInvoiceProfile)
        .filter(TenantInvoiceProfile.tenant_id == tenant_id)
        .one_or_none()
    )


def get_profile(db: Session, tenant_id: int) -> InvoiceProfileData:
    row = _row(db, tenant_id)
    if row is None:
        return InvoiceProfileData()
    return InvoiceProfileData(
        mode=row.mode,
        buyer_name=row.buyer_name,
        buyer_identifier=row.buyer_identifier,
        carrier_type=row.carrier_type,
        carrier_number=row.carrier_number,
        donation_code=row.donation_code,
    )


def _normalize(
    *,
    mode: str,
    buyer_name: str,
    buyer_identifier: str,
    carrier_type: str,
    carrier_number: str,
    donation_code: str,
) -> InvoiceProfileData:
    mode = mode.strip().lower()
    buyer_name = buyer_name.strip()
    buyer_identifier = buyer_identifier.strip()
    carrier_type = carrier_type.strip().lower()
    carrier_number = carrier_number.strip().upper()
    donation_code = donation_code.strip()

    if mode not in VALID_MODES:
        raise InvoiceProfileError("請選擇有效的發票用途。")
    if mode == MODE_DONATION:
        if not _DONATION_RE.fullmatch(donation_code):
            raise InvoiceProfileError("愛心捐贈碼必須為 3–7 碼數字。")
        return InvoiceProfileData(mode=mode, donation_code=donation_code)

    if carrier_type not in VALID_CARRIERS:
        raise InvoiceProfileError("請選擇有效的電子發票載具。")
    if carrier_type == CARRIER_ECPAY:
        carrier_number = ""
    elif carrier_type == CARRIER_MOBILE:
        if not _MOBILE_RE.fullmatch(carrier_number):
            raise InvoiceProfileError("手機條碼必須為 / 加 7 碼大寫英數或 + - . 符號。")
    elif not _CITIZEN_RE.fullmatch(carrier_number):
        raise InvoiceProfileError("自然人憑證載具必須為 2 碼大寫英文加 14 碼數字。")

    if mode == MODE_BUSINESS:
        if not _valid_identifier(buyer_identifier):
            raise InvoiceProfileError("公司統一編號必須為 8 碼數字且檢查碼正確。")
        if not buyer_name or len(buyer_name) > 60:
            raise InvoiceProfileError("公司／買受人名稱必須為 1–60 個字。")
    else:
        buyer_name = ""
        buyer_identifier = ""

    return InvoiceProfileData(
        mode=mode,
        buyer_name=buyer_name,
        buyer_identifier=buyer_identifier,
        carrier_type=carrier_type,
        carrier_number=carrier_number,
    )


def save_profile(
    db: Session,
    *,
    tenant_id: int,
    mode: str,
    buyer_name: str,
    buyer_identifier: str,
    carrier_type: str,
    carrier_number: str,
    donation_code: str,
    actor_user_id: int,
) -> TenantInvoiceProfile:
    row = _row(db, tenant_id)
    normalized_carrier_type = carrier_type.strip().lower()
    if (
        not carrier_number.strip()
        and row is not None
        and mode.strip().lower() != MODE_DONATION
        and normalized_carrier_type == row.carrier_type
        and normalized_carrier_type in {CARRIER_MOBILE, CARRIER_CITIZEN}
    ):
        carrier_number = row.carrier_number
    data = _normalize(
        mode=mode,
        buyer_name=buyer_name,
        buyer_identifier=buyer_identifier,
        carrier_type=carrier_type,
        carrier_number=carrier_number,
        donation_code=donation_code,
    )
    if row is None:
        row = TenantInvoiceProfile(tenant_id=tenant_id)
        db.add(row)
    row.mode = data.mode
    row.buyer_name = data.buyer_name
    row.buyer_identifier = data.buyer_identifier
    row.carrier_type = data.carrier_type
    row.carrier_number = data.carrier_number
    row.donation_code = data.donation_code
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def masked_carrier(carrier_type: str, carrier_number: str) -> str:
    if carrier_type == CARRIER_ECPAY:
        return "Email 電子載具"
    value = carrier_number
    if len(value) <= 4:
        return "••••"
    return f"{value[:2]}{'•' * min(8, len(value) - 4)}{value[-2:]}"


def invoice_buyer_summary(invoice) -> str:
    if (invoice.invoice_mode or "personal") == MODE_DONATION:
        return f"愛心捐贈 {invoice.donation_code or '—'}"
    carrier = masked_carrier(
        invoice.carrier_type or CARRIER_ECPAY, invoice.carrier_number
    )
    if (invoice.invoice_mode or "personal") == MODE_BUSINESS:
        return f"公司統編 {invoice.buyer_identifier or '—'} · {carrier}"
    return f"個人 · {carrier}"


def profile_status(db: Session, tenant_id: int) -> dict:
    profile = get_profile(db, tenant_id)
    return {
        "configured": _row(db, tenant_id) is not None,
        "mode": profile.mode,
        "buyer_name": profile.buyer_name,
        "buyer_identifier": profile.buyer_identifier,
        "carrier_type": profile.carrier_type,
        "has_carrier_number": bool(profile.carrier_number),
        "masked_carrier": masked_carrier(
            profile.carrier_type, profile.carrier_number
        ),
        "donation_code": profile.donation_code,
    }

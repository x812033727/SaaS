"""電子發票紀錄（C2）— 每筆成功扣款/訂單開一張,冪等以來源單據查重。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field

INVOICE_PENDING = "pending"
INVOICE_ISSUED = "issued"
INVOICE_FAILED = "failed"
INVOICE_VOIDING = "voiding"
INVOICE_VOID = "void"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 來源:訂閱逐期扣款(unique = 回調重放不重開)。order_id 預留商城/定金。
    subscription_charge_id = Column(
        Integer,
        ForeignKey("subscription_charges.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    order_id = Column(Integer, nullable=True)
    # 綠界 RelateNumber(自訂單號,≤30 英數,唯一)。
    relate_number = Column(String(30), unique=True, nullable=False)
    invoice_no = Column(String(16), nullable=True)       # 開立成功才有
    invoice_date = Column(String(32), nullable=True)
    random_number = Column(String(8), nullable=True)
    amount_cents = Column(Integer, nullable=False)
    buyer_email = Column(String(256), nullable=True)
    # 開立當下的買受資訊快照；重試不讀取之後可能已變更的店家設定。
    invoice_mode = Column(String(16), nullable=False, default="personal")
    buyer_name = Column(String(60), nullable=False, default="")
    buyer_identifier = Column(String(8), nullable=False, default="")
    carrier_type = Column(String(16), nullable=False, default="ecpay")
    carrier_number_enc = Column(LargeBinary, nullable=True)
    donation_code = Column(String(7), nullable=False, default="")
    status = Column(String(8), nullable=False, default=INVOICE_PENDING)
    provider = Column(String(8), nullable=False, default="stub")  # stub | ecpay
    error_msg = Column(String(255), nullable=True)
    void_reason = Column(String(20), nullable=True)
    void_error_msg = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    issued_at = Column(DateTime(timezone=True), nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def carrier_number(self) -> str:
        return decrypt_field(self.carrier_number_enc) if self.carrier_number_enc else ""

    @carrier_number.setter
    def carrier_number(self, value: str) -> None:
        self.carrier_number_enc = encrypt_field(value) if value else None

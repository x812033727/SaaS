"""店家電子發票買受資訊；載具號碼以 Fernet 加密保存。"""

from __future__ import annotations

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String

from saas_mvp.db import Base
from saas_mvp.models.line_channel_config import decrypt_field, encrypt_field


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class TenantInvoiceProfile(Base):
    __tablename__ = "tenant_invoice_profiles"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    mode = Column(String(16), nullable=False, default="personal")
    buyer_name = Column(String(60), nullable=False, default="")
    buyer_identifier = Column(String(8), nullable=False, default="")
    carrier_type = Column(String(16), nullable=False, default="ecpay")
    carrier_number_enc = Column(LargeBinary, nullable=True)
    donation_code = Column(String(7), nullable=False, default="")
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def carrier_number(self) -> str:
        return decrypt_field(self.carrier_number_enc) if self.carrier_number_enc else ""

    @carrier_number.setter
    def carrier_number(self, value: str) -> None:
        self.carrier_number_enc = encrypt_field(value) if value else None

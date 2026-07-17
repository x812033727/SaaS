"""店家自有電子發票設定(R5-C2)— 憑證存取 + per-tenant issuer 工廠。

與平台級 platform_invoice_config(平台開給店家的月費發票)完全分離。
opt-in 語意:enabled 且憑證齊備才回 issuer;否則 None = 完全不開票。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_mvp.models.tenant_einvoice_config import TenantEinvoiceConfig


class EinvoiceConfigError(ValueError):
    """設定驗證失敗(使用者可讀訊息)。"""


def get_config(db: Session, tenant_id: int) -> TenantEinvoiceConfig | None:
    return db.execute(
        select(TenantEinvoiceConfig).where(
            TenantEinvoiceConfig.tenant_id == tenant_id
        )
    ).scalar_one_or_none()


def save_config(
    db: Session,
    *,
    tenant_id: int,
    merchant_id: str,
    hash_key: str = "",
    hash_iv: str = "",
    environment: str = "stage",
    enabled: bool = False,
    updated_by_user_id: int | None = None,
) -> TenantEinvoiceConfig:
    """upsert 店家發票憑證。hash_key/hash_iv 留空=沿用既有值(遮罩表單慣例)。

    commit 由本函式負責(表單處理路徑)。
    """
    merchant_id = (merchant_id or "").strip()
    environment = (environment or "stage").strip()
    if environment not in ("stage", "prod"):
        raise EinvoiceConfigError("環境僅接受 stage 或 prod。")
    if enabled and not merchant_id:
        raise EinvoiceConfigError("啟用前請先填 MerchantID。")

    row = get_config(db, tenant_id)
    if row is None:
        row = TenantEinvoiceConfig(tenant_id=tenant_id)
        db.add(row)
    row.merchant_id = merchant_id
    if hash_key.strip():
        row.hash_key = hash_key.strip()
    if hash_iv.strip():
        row.hash_iv = hash_iv.strip()
    row.environment = environment
    if enabled and not (
        merchant_id and row.hash_key_enc and row.hash_iv_enc
    ):
        raise EinvoiceConfigError(
            "啟用前請先填齊 MerchantID / HashKey / HashIV。"
        )
    row.enabled = enabled
    row.updated_by_user_id = updated_by_user_id
    db.commit()
    db.refresh(row)
    return row


def issuer_for_tenant(db: Session, tenant_id: int):
    """回傳該店家的發票 issuer;未啟用/憑證不齊回 None(=不開票)。"""
    config = get_config(db, tenant_id)
    if config is None or not config.enabled or not config.is_complete:
        return None
    from saas_mvp.services.invoice_ecpay import EcpayInvoiceIssuer

    return EcpayInvoiceIssuer(
        merchant_id=config.merchant_id,
        hash_key=config.hash_key,
        hash_iv=config.hash_iv,
        env=config.environment,
    )


def einvoice_enabled(db: Session, tenant_id: int) -> bool:
    config = get_config(db, tenant_id)
    return bool(config is not None and config.enabled and config.is_complete)

"""平台電子發票設定：資料庫優先、環境變數備援。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from saas_mvp.models.platform_invoice_config import PlatformInvoiceConfig

_ECPAY_TEST_MERCHANT = "2000132"
_MERCHANT_RE = re.compile(r"^[A-Za-z0-9]{5,10}$")


class PlatformInvoiceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveInvoiceConfig:
    provider: str
    environment: str
    merchant_id: str
    hash_key: str
    hash_iv: str
    source: str


def _row(db: Session) -> PlatformInvoiceConfig | None:
    return db.get(PlatformInvoiceConfig, 1)


def effective_invoice_config(db: Session | None, settings) -> EffectiveInvoiceConfig:
    if db is not None:
        row = _row(db)
        if row is not None:
            return EffectiveInvoiceConfig(
                provider=row.provider,
                environment=row.environment,
                merchant_id=row.merchant_id,
                hash_key=row.hash_key,
                hash_iv=row.hash_iv,
                source="database",
            )
    return EffectiveInvoiceConfig(
        provider=(settings.invoice_provider or "stub").strip().lower(),
        environment=(settings.ecpay_invoice_env or "stage").strip().lower(),
        merchant_id=(settings.ecpay_invoice_merchant_id or "").strip(),
        hash_key=settings.ecpay_invoice_hash_key or "",
        hash_iv=settings.ecpay_invoice_hash_iv or "",
        source="environment",
    )


def invoice_status(db: Session, settings) -> dict:
    config = effective_invoice_config(db, settings)
    row = _row(db)
    return {
        "provider": config.provider,
        "configured": config.provider == "ecpay",
        "source": config.source,
        "environment": config.environment,
        "merchant_id": config.merchant_id,
        "has_hash_key": bool(config.hash_key),
        "has_hash_iv": bool(config.hash_iv),
        "updated_at": row.updated_at if config.source == "database" and row else None,
    }


def _valid_aes_secret(value: str) -> bool:
    return len(value.encode("utf-8")) == 16 and not any(ch.isspace() for ch in value)


def _retryable_invoice_count(db: Session) -> int:
    from saas_mvp.models.invoice import INVOICE_FAILED, INVOICE_PENDING, Invoice

    return db.query(Invoice).filter(
        Invoice.status.in_((INVOICE_PENDING, INVOICE_FAILED))
    ).count()


def _open_ecpay_invoice_count(db: Session) -> int:
    from saas_mvp.models.invoice import INVOICE_ISSUED, INVOICE_VOIDING, Invoice

    return db.query(Invoice).filter(
        Invoice.provider == "ecpay",
        Invoice.status.in_((INVOICE_ISSUED, INVOICE_VOIDING)),
    ).count()


def _ensure_safe_change(db: Session, current, next_values: tuple[str, str, str, str]) -> None:
    if not _retryable_invoice_count(db):
        return
    if next_values != (
        current.merchant_id,
        current.environment,
        current.hash_key,
        current.hash_iv,
    ):
        raise PlatformInvoiceConfigError(
            "仍有等待開立或開立失敗的發票，請先重試或人工處理後再更換憑證。"
        )


def save_ecpay_config(
    db: Session,
    *,
    merchant_id: str,
    hash_key: str,
    hash_iv: str,
    environment: str,
    actor_user_id: int,
) -> PlatformInvoiceConfig:
    merchant_id = merchant_id.strip()
    hash_key = hash_key.strip()
    hash_iv = hash_iv.strip()
    environment = environment.strip().lower()
    row = _row(db)

    if not _MERCHANT_RE.fullmatch(merchant_id):
        raise PlatformInvoiceConfigError("綠界發票 MerchantID 格式不正確（5–10 碼英數字）。")
    if environment not in {"stage", "prod"}:
        raise PlatformInvoiceConfigError("發票環境只能選測試或正式。")
    existing_key = row.hash_key if row is not None else ""
    existing_iv = row.hash_iv if row is not None else ""
    next_key = hash_key or existing_key
    next_iv = hash_iv or existing_iv
    if not next_key:
        raise PlatformInvoiceConfigError("首次設定必須輸入發票 HashKey。")
    if not next_iv:
        raise PlatformInvoiceConfigError("首次設定必須輸入發票 HashIV。")
    if not _valid_aes_secret(next_key):
        raise PlatformInvoiceConfigError("發票 HashKey 必須恰好為 16 bytes 且不可含空白。")
    if not _valid_aes_secret(next_iv):
        raise PlatformInvoiceConfigError("發票 HashIV 必須恰好為 16 bytes 且不可含空白。")
    if environment == "prod" and merchant_id == _ECPAY_TEST_MERCHANT:
        raise PlatformInvoiceConfigError("正式環境不可使用綠界公開測試 MerchantID 2000132。")

    from saas_mvp.config import settings

    current = effective_invoice_config(db, settings)
    _ensure_safe_change(db, current, (merchant_id, environment, next_key, next_iv))
    if _open_ecpay_invoice_count(db) and (
        merchant_id != current.merchant_id or environment != current.environment
    ):
        raise PlatformInvoiceConfigError(
            "仍有尚未作廢的綠界發票；不可更換發票 MerchantID 或環境，否則將無法作廢舊發票。"
        )

    if row is None:
        row = PlatformInvoiceConfig(id=1, provider="ecpay")
        row.hash_key = next_key
        row.hash_iv = next_iv
        db.add(row)
    else:
        if hash_key:
            row.hash_key = hash_key
        if hash_iv:
            row.hash_iv = hash_iv
    row.provider = "ecpay"
    row.environment = environment
    row.merchant_id = merchant_id
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def disable_invoice(db: Session, *, actor_user_id: int) -> PlatformInvoiceConfig:
    if _retryable_invoice_count(db):
        raise PlatformInvoiceConfigError(
            "仍有等待開立或開立失敗的發票，請先處理完成再停用電子發票。"
        )
    row = _row(db)
    if row is None:
        from saas_mvp.config import settings

        current = effective_invoice_config(db, settings)
        row = PlatformInvoiceConfig(
            id=1,
            provider="stub",
            environment=current.environment,
            merchant_id=current.merchant_id,
        )
        # 從環境備援停用時仍須保留一份加密憑證，否則先前已開立的發票
        # 會因 provider 切成 stub 而失去日後作廢所需的原商店資料。
        row.hash_key = current.hash_key
        row.hash_iv = current.hash_iv
        db.add(row)
    row.provider = "stub"
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def clear_invoice_override(db: Session) -> bool:
    if _retryable_invoice_count(db):
        raise PlatformInvoiceConfigError(
            "仍有等待開立或開立失敗的發票，不能移除目前設定。"
        )
    if _open_ecpay_invoice_count(db):
        raise PlatformInvoiceConfigError(
            "仍有尚未作廢的綠界發票，不能移除作廢時所需的發票憑證。"
        )
    row = _row(db)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def self_check(db: Session, settings) -> None:
    config = effective_invoice_config(db, settings)
    if config.provider != "ecpay":
        raise PlatformInvoiceConfigError("綠界電子發票尚未啟用。")
    if not _MERCHANT_RE.fullmatch(config.merchant_id):
        raise PlatformInvoiceConfigError("發票 MerchantID 格式不正確。")
    if not (_valid_aes_secret(config.hash_key) and _valid_aes_secret(config.hash_iv)):
        raise PlatformInvoiceConfigError("發票 HashKey 或 HashIV 不完整。")
    if config.environment == "prod" and config.merchant_id == _ECPAY_TEST_MERCHANT:
        raise PlatformInvoiceConfigError("正式環境仍使用公開測試 MerchantID。")

    from saas_mvp.services.invoice_ecpay import aes_decrypt_data, aes_encrypt_data

    probe = {"MerchantID": config.merchant_id, "RelateNumber": "SaaSConfigCheck"}
    encrypted = aes_encrypt_data(probe, config.hash_key, config.hash_iv)
    if aes_decrypt_data(encrypted, config.hash_key, config.hash_iv) != probe:
        raise PlatformInvoiceConfigError("發票 AES 加解密自我檢查失敗。")

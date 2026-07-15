"""平台金流設定：資料庫優先、環境變數備援。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from saas_mvp.config import settings
from saas_mvp.models.platform_payment_config import PlatformPaymentConfig

_ECPAY_TEST_MERCHANT = "2000132"
_MERCHANT_RE = re.compile(r"^[A-Za-z0-9]{5,64}$")


class PlatformPaymentConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EffectivePaymentConfig:
    provider: str
    environment: str
    merchant_id: str
    hash_key: str
    hash_iv: str
    source: str


def _row(db: Session) -> PlatformPaymentConfig | None:
    return db.get(PlatformPaymentConfig, 1)


def effective_payment_config(db: Session | None, settings) -> EffectivePaymentConfig:
    if db is not None:
        row = _row(db)
        if row is not None:
            return EffectivePaymentConfig(
                provider=row.provider,
                environment=row.environment,
                merchant_id=row.merchant_id,
                hash_key=row.hash_key,
                hash_iv=row.hash_iv,
                source="database",
            )
    return EffectivePaymentConfig(
        provider=(settings.payment_provider or "stub").strip().lower(),
        environment=(settings.ecpay_env or "stage").strip().lower(),
        merchant_id=(settings.ecpay_merchant_id or "").strip(),
        hash_key=settings.ecpay_hash_key or "",
        hash_iv=settings.ecpay_hash_iv or "",
        source="environment",
    )


def payment_provider(db: Session | None, settings) -> str:
    return effective_payment_config(db, settings).provider


def payment_status(db: Session, settings) -> dict:
    config = effective_payment_config(db, settings)
    return {
        "provider": config.provider,
        "configured": config.provider == "ecpay",
        "source": config.source,
        "environment": config.environment,
        "merchant_id": config.merchant_id,
        "has_hash_key": bool(config.hash_key),
        "has_hash_iv": bool(config.hash_iv),
        "updated_at": _row(db).updated_at if config.source == "database" else None,
    }


def _valid_secret(value: str) -> bool:
    return 8 <= len(value) <= 128 and not any(ch.isspace() for ch in value)


def _unsettled_subscription_count(db: Session) -> int:
    from saas_mvp.models.feature_subscription import (
        SUB_ACTIVE,
        SUB_CANCEL_FAILED,
        SUB_PENDING,
        FeatureSubscription,
    )

    return db.query(FeatureSubscription).filter(
        FeatureSubscription.status.in_((SUB_PENDING, SUB_ACTIVE, SUB_CANCEL_FAILED))
    ).count()


def refundable_deposit_count(db: Session) -> int:
    """仍可能退款的已付定金；完成到場或退款後才可安全輪替原商店憑證。"""
    from saas_mvp.models.reservation import Reservation

    return (
        db.query(Reservation)
        .filter(
            Reservation.deposit_status == "paid",
            Reservation.attended.is_not(True),
        )
        .count()
    )


def save_ecpay_config(
    db: Session,
    *,
    merchant_id: str,
    hash_key: str,
    hash_iv: str,
    environment: str,
    actor_user_id: int,
    public_base_url: str,
) -> PlatformPaymentConfig:
    merchant_id = merchant_id.strip()
    hash_key = hash_key.strip()
    hash_iv = hash_iv.strip()
    environment = environment.strip().lower()
    row = _row(db)

    if not _MERCHANT_RE.fullmatch(merchant_id):
        raise PlatformPaymentConfigError("綠界商店代號格式不正確。")
    if environment not in {"stage", "prod"}:
        raise PlatformPaymentConfigError("金流環境只能選測試或正式。")
    existing_hash_key = row.hash_key if row is not None else ""
    existing_hash_iv = row.hash_iv if row is not None else ""
    if not hash_key and not existing_hash_key:
        raise PlatformPaymentConfigError("首次設定必須輸入 HashKey。")
    if not hash_iv and not existing_hash_iv:
        raise PlatformPaymentConfigError("首次設定必須輸入 HashIV。")
    if not _valid_secret(hash_key or existing_hash_key):
        raise PlatformPaymentConfigError("HashKey 格式不正確。")
    if not _valid_secret(hash_iv or existing_hash_iv):
        raise PlatformPaymentConfigError("HashIV 格式不正確。")
    if environment == "prod" and merchant_id == _ECPAY_TEST_MERCHANT:
        raise PlatformPaymentConfigError("正式環境不可使用綠界測試商店代號 2000132。")
    if not public_base_url.startswith("https://"):
        raise PlatformPaymentConfigError("綠界金流需要 HTTPS 對外網址才能接收付款通知。")

    # 未結清訂閱的回調與停扣都依賴原商店憑證；禁止直接輪替或切環境，避免
    # 已收款訂閱無法驗簽、或停扣 API 打到錯誤商店。
    if _unsettled_subscription_count(db):
        current = effective_payment_config(db, settings)
        next_hash_key = hash_key or current.hash_key
        next_hash_iv = hash_iv or current.hash_iv
        if (
            merchant_id != current.merchant_id
            or environment != current.environment
            or next_hash_key != current.hash_key
            or next_hash_iv != current.hash_iv
        ):
            raise PlatformPaymentConfigError(
                "仍有待付款、扣款中或停扣失敗的訂閱，請先處理完成再更換綠界憑證。"
            )
    if refundable_deposit_count(db):
        current = effective_payment_config(db, settings)
        next_hash_key = hash_key or current.hash_key
        next_hash_iv = hash_iv or current.hash_iv
        if (
            merchant_id != current.merchant_id
            or environment != current.environment
            or next_hash_key != current.hash_key
            or next_hash_iv != current.hash_iv
        ):
            raise PlatformPaymentConfigError(
                "仍有尚未完成到場或退款的已付定金，請先處理後再更換綠界憑證。"
            )

    if row is None:
        row = PlatformPaymentConfig(id=1, provider="ecpay")
        row.hash_key = hash_key
        row.hash_iv = hash_iv
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


def disable_payment(db: Session, *, actor_user_id: int) -> PlatformPaymentConfig:
    if _unsettled_subscription_count(db):
        raise PlatformPaymentConfigError(
            "仍有待付款、扣款中或停扣失敗的訂閱，請先完成退訂與停扣後再停用金流。"
        )
    if refundable_deposit_count(db):
        raise PlatformPaymentConfigError(
            "仍有尚未完成到場或退款的已付定金，不能停用目前金流。"
        )
    row = _row(db)
    if row is None:
        row = PlatformPaymentConfig(
            id=1,
            provider="stub", environment="stage", merchant_id=""
        )
        row.hash_key = ""
        row.hash_iv = ""
        db.add(row)
    row.provider = "stub"
    row.updated_by_user_id = actor_user_id
    db.flush()
    return row


def clear_payment_override(db: Session) -> bool:
    if _unsettled_subscription_count(db):
        raise PlatformPaymentConfigError(
            "仍有待付款、扣款中或停扣失敗的訂閱，不能移除目前金流設定。"
        )
    if refundable_deposit_count(db):
        raise PlatformPaymentConfigError(
            "仍有尚未完成到場或退款的已付定金，不能移除目前金流設定。"
        )
    row = _row(db)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def self_check(db: Session, settings) -> None:
    config = effective_payment_config(db, settings)
    if config.provider != "ecpay":
        raise PlatformPaymentConfigError("綠界金流尚未啟用。")
    if not _MERCHANT_RE.fullmatch(config.merchant_id):
        raise PlatformPaymentConfigError("商店代號格式不正確。")
    if not (_valid_secret(config.hash_key) and _valid_secret(config.hash_iv)):
        raise PlatformPaymentConfigError("HashKey 或 HashIV 不完整。")
    if config.environment == "prod" and config.merchant_id == _ECPAY_TEST_MERCHANT:
        raise PlatformPaymentConfigError("正式環境仍使用測試商店代號。")
    if not settings.public_base_url.startswith("https://"):
        raise PlatformPaymentConfigError("缺少 HTTPS 對外網址。")

    from saas_mvp.services.payment_ecpay import EcpayClient

    client = EcpayClient(
        merchant_id=config.merchant_id,
        hash_key=config.hash_key,
        hash_iv=config.hash_iv,
        env=config.environment,
    )
    probe = {
        "MerchantID": config.merchant_id,
        "MerchantTradeNo": "SaaSConfigCheck",
        "TotalAmount": "1",
        "EncryptType": "1",
    }
    probe["CheckMacValue"] = client.check_mac_value(probe)
    if not client.verify(probe):  # pragma: no cover - invariant guard
        raise PlatformPaymentConfigError("綠界簽章自我檢查失敗。")

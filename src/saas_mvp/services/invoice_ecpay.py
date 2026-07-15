"""電子發票 issuer（C2）— Stub + 綠界 B2C 雙模式。

⚠️ 綠界**電子發票 API 與金流 AIO 完全不同**:JSON API,`Data` 欄位為
「JSON → URL-encode → AES-128-CBC(HashKey/HashIV) → base64」,非 CheckMacValue;
發票的 MerchantID/HashKey/HashIV 也是**獨立一組**(發票商店),與金流不共用。
端點:stage `https://einvoice-stage.ecpay.com.tw/B2CInvoice/Issue`、
prod `https://einvoice.ecpay.com.tw/B2CInvoice/Issue`。

比照 payment_ecpay.py 慣例:stdlib urllib、`_urllib_post` 可注入、不引 SDK。
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import json
import time
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy.orm import Session

from saas_mvp.config import settings

_STAGE_URL = "https://einvoice-stage.ecpay.com.tw/B2CInvoice/Issue"
_PROD_URL = "https://einvoice.ecpay.com.tw/B2CInvoice/Issue"
_STAGE_INVALID_URL = "https://einvoice-stage.ecpay.com.tw/B2CInvoice/Invalid"
_PROD_INVALID_URL = "https://einvoice.ecpay.com.tw/B2CInvoice/Invalid"


class InvoiceError(Exception):
    """開立失敗(網路/API/解密錯誤統一包裝)。"""


@dataclasses.dataclass(frozen=True)
class IssueResult:
    invoice_no: str
    invoice_date: str
    random_number: str
    raw: dict


@dataclasses.dataclass(frozen=True)
class VoidResult:
    invoice_no: str
    raw: dict


class InvoiceIssuer:
    """介面:issue() 成功回 IssueResult,失敗拋 InvoiceError。"""

    def issue(
        self,
        *,
        relate_number: str,
        amount_twd: int,
        buyer_email: str,
        item_name: str,
        buyer_name: str = "",
        buyer_identifier: str = "",
        carrier_type: str = "ecpay",
        carrier_number: str = "",
        donation_code: str = "",
    ) -> IssueResult:
        raise NotImplementedError

    def void(
        self, *, invoice_no: str, invoice_date: str, reason: str
    ) -> VoidResult:
        raise NotImplementedError


class StubInvoiceIssuer(InvoiceIssuer):
    """離線 stub:決定性假號(ST + relate hash),issued 清單供測試斷言。"""

    def __init__(self) -> None:
        self.issued: list[dict] = []
        self.voided: list[dict] = []

    def issue(
        self,
        *,
        relate_number: str,
        amount_twd: int,
        buyer_email: str,
        item_name: str,
        buyer_name: str = "",
        buyer_identifier: str = "",
        carrier_type: str = "ecpay",
        carrier_number: str = "",
        donation_code: str = "",
    ) -> IssueResult:
        digest = hashlib.sha1(relate_number.encode()).hexdigest()[:8].upper()
        record = {
            "relate_number": relate_number,
            "amount_twd": amount_twd,
            "buyer_email": buyer_email,
            "item_name": item_name,
            "buyer_name": buyer_name,
            "buyer_identifier": buyer_identifier,
            "carrier_type": carrier_type,
            "carrier_number": carrier_number,
            "donation_code": donation_code,
        }
        self.issued.append(record)
        return IssueResult(
            invoice_no=f"ST{digest}",
            invoice_date=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
            random_number=digest[:4],
            raw=record,
        )

    def void(
        self, *, invoice_no: str, invoice_date: str, reason: str
    ) -> VoidResult:
        record = {
            "invoice_no": invoice_no,
            "invoice_date": invoice_date,
            "reason": reason,
        }
        self.voided.append(record)
        return VoidResult(invoice_no=invoice_no, raw=record)


def _urllib_post(url: str, body: bytes) -> str:
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def aes_encrypt_data(payload: dict, hash_key: str, hash_iv: str) -> str:
    """綠界發票 Data 加密:JSON → URL-encode → AES-128-CBC → base64。"""
    plain = urllib.parse.quote(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ).encode()
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(hash_key.encode()), modes.CBC(hash_iv.encode())
    ).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def aes_decrypt_data(data_b64: str, hash_key: str, hash_iv: str) -> dict:
    """綠界發票 Data 解密(base64 → AES → unquote → JSON)。"""
    ct = base64.b64decode(data_b64)
    decryptor = Cipher(
        algorithms.AES(hash_key.encode()), modes.CBC(hash_iv.encode())
    ).decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return json.loads(urllib.parse.unquote(plain.decode()))


class EcpayInvoiceIssuer(InvoiceIssuer):
    """綠界 B2C 發票：個人／統編、電子載具或愛心捐贈，金額含稅。"""

    def __init__(
        self,
        *,
        merchant_id: str | None = None,
        hash_key: str | None = None,
        hash_iv: str | None = None,
        env: str | None = None,
        http_post=None,
    ) -> None:
        self._merchant_id = (
            settings.ecpay_invoice_merchant_id if merchant_id is None else merchant_id
        )
        self._hash_key = settings.ecpay_invoice_hash_key if hash_key is None else hash_key
        self._hash_iv = settings.ecpay_invoice_hash_iv if hash_iv is None else hash_iv
        effective_env = settings.ecpay_invoice_env if env is None else env
        self._environment = effective_env
        self._url = _PROD_URL if effective_env == "prod" else _STAGE_URL
        self._invalid_url = (
            _PROD_INVALID_URL if effective_env == "prod" else _STAGE_INVALID_URL
        )
        self._http_post = http_post or _urllib_post

    def issue(
        self,
        *,
        relate_number: str,
        amount_twd: int,
        buyer_email: str,
        item_name: str,
        buyer_name: str = "",
        buyer_identifier: str = "",
        carrier_type: str = "ecpay",
        carrier_number: str = "",
        donation_code: str = "",
    ) -> IssueResult:
        if not (self._merchant_id and self._hash_key and self._hash_iv):
            raise InvoiceError("ecpay invoice credentials not configured")
        carrier_codes = {"ecpay": "1", "citizen": "2", "mobile": "3"}
        if carrier_type not in carrier_codes:
            raise InvoiceError("unsupported invoice carrier type")
        is_donation = bool(donation_code)
        if self._environment != "prod":
            # 綠界明確要求 Stage 不帶真實個資；保留相同欄位組合與合法格式驗證。
            buyer_email = "test@ecpay.com.tw"
            buyer_name = "綠界科技股份有限公司" if buyer_name else ""
            buyer_identifier = "97025978" if buyer_identifier else ""
            donation_code = "168001" if is_donation else ""
            if carrier_type == "mobile":
                carrier_number = "/ABC1234"
            elif carrier_type == "citizen":
                carrier_number = "AB12345678901234"
        data = {
            "MerchantID": self._merchant_id,
            "RelateNumber": relate_number,
            "CustomerIdentifier": "" if is_donation else buyer_identifier,
            "CustomerName": "" if is_donation else buyer_name,
            "CustomerEmail": buyer_email,
            "Print": "0",
            "Donation": "1" if is_donation else "0",
            "LoveCode": donation_code if is_donation else "",
            "CarrierType": "" if is_donation else carrier_codes[carrier_type],
            "CarrierNum": "" if is_donation or carrier_type == "ecpay" else carrier_number,
            "TaxType": "1",           # 應稅
            "SalesAmount": amount_twd,  # 含稅總額
            "InvType": "07",
            "Items": [{
                "ItemName": item_name[:100],
                "ItemCount": 1,
                "ItemWord": "式",
                "ItemPrice": amount_twd,
                "ItemAmount": amount_twd,
            }],
        }
        envelope = {
            "MerchantID": self._merchant_id,
            "RqHeader": {"Timestamp": int(time.time())},
            "Data": aes_encrypt_data(data, self._hash_key, self._hash_iv),
        }
        try:
            raw = self._http_post(self._url, json.dumps(envelope).encode())
            resp = json.loads(raw)
            if str(resp.get("TransCode")) != "1":
                raise InvoiceError(f"transport error: {resp.get('TransMsg')}")
            payload = aes_decrypt_data(resp["Data"], self._hash_key, self._hash_iv)
        except InvoiceError:
            raise
        except Exception as exc:  # noqa: BLE001 — 統一包裝
            raise InvoiceError(f"ecpay invoice request failed: {exc}") from exc

        if str(payload.get("RtnCode")) != "1":
            raise InvoiceError(
                f"issue rejected: {payload.get('RtnCode')} {payload.get('RtnMsg')}"
            )
        return IssueResult(
            invoice_no=str(payload.get("InvoiceNo") or ""),
            invoice_date=str(payload.get("InvoiceDate") or ""),
            random_number=str(payload.get("RandomNumber") or ""),
            raw=payload,
        )

    def void(
        self, *, invoice_no: str, invoice_date: str, reason: str
    ) -> VoidResult:
        if not (self._merchant_id and self._hash_key and self._hash_iv):
            raise InvoiceError("ecpay invoice credentials not configured")
        data = {
            "MerchantID": self._merchant_id,
            "InvoiceNo": invoice_no,
            "InvoiceDate": invoice_date,
            "Reason": reason,
        }
        envelope = {
            "MerchantID": self._merchant_id,
            "RqHeader": {"Timestamp": int(time.time())},
            "Data": aes_encrypt_data(data, self._hash_key, self._hash_iv),
        }
        try:
            raw = self._http_post(self._invalid_url, json.dumps(envelope).encode())
            resp = json.loads(raw)
            if str(resp.get("TransCode")) != "1":
                raise InvoiceError(f"transport error: {resp.get('TransMsg')}")
            payload = aes_decrypt_data(resp["Data"], self._hash_key, self._hash_iv)
        except InvoiceError:
            raise
        except Exception as exc:  # noqa: BLE001 — 統一包裝
            raise InvoiceError(f"ecpay invoice void request failed: {exc}") from exc

        if str(payload.get("RtnCode")) != "1":
            raise InvoiceError(
                f"void rejected: {payload.get('RtnCode')} {payload.get('RtnMsg')}"
            )
        returned_no = str(payload.get("InvoiceNo") or "")
        if returned_no != invoice_no:
            raise InvoiceError("void response invoice number mismatch")
        return VoidResult(invoice_no=returned_no, raw=payload)


_stub_singleton = StubInvoiceIssuer()


def get_invoice_issuer(
    db: Session | None = None, *, provider: str | None = None
) -> InvoiceIssuer:
    """資料庫後台設定優先；未建立設定時才使用環境變數備援。"""
    from saas_mvp.services.platform_invoice_config import effective_invoice_config

    config = effective_invoice_config(db, settings)
    requested_provider = provider or config.provider
    if requested_provider == "ecpay":
        return EcpayInvoiceIssuer(
            merchant_id=config.merchant_id,
            hash_key=config.hash_key,
            hash_iv=config.hash_iv,
            env=config.environment,
        )
    return _stub_singleton

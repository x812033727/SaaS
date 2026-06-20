"""Real HTTP translation backend (DeepL-compatible API).

Uses stdlib ``urllib`` only — no extra runtime dependencies.
Raises ``TranslationError`` on any failure so the caller can decide
whether to fall back to the stub or surface an error to the user.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from saas_mvp.translation.base import TranslationResult, Translator, TranslationError

_DEEPL_FREE_URL = "https://api-free.deepl.com/v2/translate"

# BCP-47 tag → DeepL 接受的 target_lang。DeepL 不接受 ZH-TW/ZH-CN（會回 400），
# 繁/簡須映射成 ZH-HANT/ZH-HANS；其餘語言（JA/EN/KO…）直接 .upper() 即可。
_DEEPL_LANG_MAP = {
    "ZH-TW": "ZH-HANT",
    "ZH-CN": "ZH-HANS",
}

# DeepL 對中文偵測只回 "ZH"，不細分繁簡；因此同語言比對時，detected="ZH"
# 視為等同於任一中文 target（ZH/ZH-HANT/ZH-HANS）。
_ZH_NORM_TARGETS = {"ZH", "ZH-HANT", "ZH-HANS"}


class DeepLTranslator(Translator):
    """Translation backend that calls the DeepL REST API.

    Configured with an ``api_key`` (SAAS_DEEPL_API_KEY env var).
    If no key is provided the instance reports ``is_available() == False``
    and callers should fall back to :class:`StubTranslator`.

    Args:
        api_key: DeepL authentication key.
        api_url: API endpoint URL (override for testing or paid tier).
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        api_url: str = _DEEPL_FREE_URL,
        timeout: int = 10,
    ) -> None:
        self._api_key = api_key
        self._api_url = api_url
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    @staticmethod
    def _normalize_target_lang(target_lang: str) -> str:
        """將 BCP-47 tag 正規化為 DeepL 接受的 target_lang。

        白名單映射不相容 tag（ZH-TW→ZH-HANT、ZH-CN→ZH-HANS），其餘 ``.upper()``
        不變（JA→JA、en→EN、ko→KO …）。
        DeepL 不接受 ZH-TW/ZH-CN，未映射會回 400 Bad Request。
        """
        upper = target_lang.upper()
        return _DEEPL_LANG_MAP.get(upper, upper)

    @staticmethod
    def _is_same_language(detected: str, norm: str) -> bool:
        """偵測到的來源語言是否等同於正規化後的 target。

        DeepL 對中文偵測只回 "ZH"（不分繁簡），故 detected="ZH" 時，
        只要 target 為任一中文 tag（ZH/ZH-HANT/ZH-HANS）即視為同語言；
        其餘語言直接做大小寫不敏感的相等比對。
        """
        d = detected.upper()
        n = norm.upper()
        if d == "ZH":
            return n in _ZH_NORM_TARGETS
        if n == "ZH":
            return d in _ZH_NORM_TARGETS
        return d == n

    def translate(self, text: str, target_lang: str) -> TranslationResult:
        """Call DeepL API and return translated text with metadata.

        若 DeepL 回傳的 ``detected_source_language`` 等同於正規化後的 target，
        代表來源語言已是目標語言，直接回傳原文（避免把同語言翻譯結果回覆給用戶）。

        Raises:
            TranslationError: on network error, HTTP error, or unexpected response.
        """
        # 單一變數防呆：API payload 與下方 skip 比較均使用 norm，避免兩處各自
        # .upper() 造成靜默不一致（漏改一處會繁中 400 或 skip 誤判）。
        norm = self._normalize_target_lang(target_lang)

        payload = urllib.parse.urlencode(
            {
                "auth_key": self._api_key,
                "text": text,
                "target_lang": norm,
            }
        ).encode()

        req = urllib.request.Request(
            self._api_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise TranslationError(
                f"DeepL HTTP {exc.code}: {exc.reason}"
            ) from exc
        except OSError as exc:
            # Covers URLError (network unreachable, timeout, connection refused …)
            raise TranslationError(f"DeepL request failed: {exc}") from exc
        except Exception as exc:
            raise TranslationError(f"Unexpected error from DeepL: {exc}") from exc

        # 同語言 skip：偵測到的來源語言等同於正規化後的 target → 回傳原文，
        # 避免把同語言翻譯結果回覆給用戶（UX 正確性）。中文場景透過
        # _is_same_language 處理 DeepL 只回 "ZH" 的限制（繁中→繁中亦會 skip）。
        # 涵蓋 AttributeError/TypeError：若 translations[0] 非 dict（格式異常），
        # 一律包成 TranslationError，維持原有錯誤封裝語意。
        try:
            translation = body["translations"][0]
            detected_raw = translation.get("detected_source_language")
            detected = (
                detected_raw
                if isinstance(detected_raw, str) and detected_raw
                else None
            )
            if detected is not None and self._is_same_language(detected, norm):
                return TranslationResult(
                    text=text,
                    detected_lang=detected,
                    skipped=True,
                )
            return TranslationResult(
                text=translation["text"],
                detected_lang=detected,
                skipped=False,
            )
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            raise TranslationError(
                f"Unexpected DeepL response structure: {body!r}"
            ) from exc

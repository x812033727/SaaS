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

from saas_mvp.translation.base import Translator, TranslationError

_DEEPL_FREE_URL = "https://api-free.deepl.com/v2/translate"

# BCP-47 tag → DeepL 接受的 target_lang。DeepL 不接受 ZH-TW/ZH-CN（會回 400），
# 須映射成 ZH-HANT/ZH-HANS。其餘語言 .upper() 即可。
_DEEPL_LANG_MAP = {
    "ZH-TW": "ZH-HANT",
    "ZH-CN": "ZH-HANS",
}


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

        白名單映射不相容 tag（ZH-TW→ZH-HANT、ZH-CN→ZH-HANS），其餘 ``.upper()``。
        """
        upper = target_lang.upper()
        return _DEEPL_LANG_MAP.get(upper, upper)

    def translate(self, text: str, target_lang: str) -> str:
        """Call DeepL API and return translated text.

        Raises:
            TranslationError: on network error, HTTP error, or unexpected response.
        """
        # 單一變數：payload 與 skip 比較均用 norm，消除兩處各自 .upper() 的不一致風險。
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

        try:
            translation = body["translations"][0]
            detected = translation.get("detected_source_language", "")
        except (KeyError, IndexError) as exc:
            raise TranslationError(
                f"Unexpected DeepL response structure: {body!r}"
            ) from exc

        # 同語言 skip：偵測到的來源語言等於正規化後的 target → 回傳原文，
        # 避免把同語言翻譯結果回覆給用戶（UX 正確性）。
        # NOTE: DeepL 對中文偵測回傳 "ZH"，而正規化後的 target 為 "ZH-HANT"，兩者不等，
        #       因此繁中→繁中不會觸發此 skip；此為 DeepL API 行為限制，非 bug。
        if detected.upper() == norm:
            return text

        try:
            return translation["text"]
        except KeyError as exc:
            raise TranslationError(
                f"Unexpected DeepL response structure: {body!r}"
            ) from exc

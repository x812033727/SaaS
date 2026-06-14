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

    def translate(self, text: str, target_lang: str) -> str:
        """Call DeepL API and return translated text.

        Raises:
            TranslationError: on network error, HTTP error, or unexpected response.
        """
        payload = urllib.parse.urlencode(
            {
                "auth_key": self._api_key,
                "text": text,
                "target_lang": target_lang.upper(),
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
            return body["translations"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise TranslationError(
                f"Unexpected DeepL response structure: {body!r}"
            ) from exc

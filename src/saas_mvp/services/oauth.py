"""OAuth 登入 provider 抽象（PHASE 3：LINE Login + Google）。

比照 translation 套件的 ABC + stub + factory 模式：
  * OAuthProvider     — 抽象介面（authorize_url / exchange_code）。
  * LineLoginProvider — 真實 LINE Login（OAuth2 + OIDC id_token）。
  * GoogleOAuthProvider — 真實 Google OAuth2（OIDC id_token + userinfo）。
  * StubOAuthProvider — 決定性離線 stub，exchange_code(code) 由 code 推導 email。
  * get_provider(name, *, settings) — 真實 client_id/secret 已設則回真實 provider，
    否則回 StubOAuthProvider（與 translation.get_translator 同型）。

真實 provider 僅用 stdlib urllib（無新增 runtime 相依，比照 line_client/http.py）。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod


# provider 名稱白名單（router 用）。
VALID_PROVIDERS: frozenset[str] = frozenset({"line", "google"})


def provider_credentials_present(name: str, *, settings, db=None) -> bool:
    """Return configuration status without exposing credential values."""
    if name == "line":
        from saas_mvp.services.platform_oauth_config import effective_line_credentials

        return effective_line_credentials(db, settings) is not None
    if name == "google":
        from saas_mvp.services.platform_oauth_config import effective_google_credentials

        return effective_google_credentials(db, settings) is not None
    return False


class OAuthError(Exception):
    """OAuth provider 失敗（網路、token 交換、回應格式異常等）。"""


class OAuthNotConfigured(OAuthError):
    """Production OAuth credentials are missing."""


class OAuthProvider(ABC):
    """OAuth provider 抽象介面。所有 backend（stub / LINE / Google）皆實作此介面。"""

    @abstractmethod
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """回傳導向 provider 授權頁的完整 URL（夾帶 state CSRF token）。"""

    @abstractmethod
    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """以授權碼換取使用者身分。

        Returns:
            dict 含 ``email``（str | None）、``subject``（provider 端穩定使用者 ID）、
            ``name``（str | None）。

        Raises:
            OAuthError: 交換失敗或回應缺必要欄位。
        """


# ── 決定性離線 stub（測試 / dev 預設） ────────────────────────────────────────


class StubOAuthProvider(OAuthProvider):
    """離線決定性 stub：不連網，由授權碼直接推導身分。

    保證：
      * exchange_code(code) → {email: f"{code}@example.com", subject: f"stub-{code}",
        name: code}，同 code 永遠同輸出。
      * authorize_url 回固定可預期的 URL（夾帶 state / redirect_uri）。

    無 client_id/secret 設定時為預設 provider，亦為測試的標準實作。
    """

    def __init__(self, name: str = "stub", *, email_verified: bool = True) -> None:
        self._name = name
        # 測試旋鈕：模擬 provider 回未驗證 email（email_verified=False）。
        self._email_verified = email_verified

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        params = urllib.parse.urlencode({"state": state, "redirect_uri": redirect_uri})
        return f"https://stub-oauth.local/{self._name}/authorize?{params}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        if not code:
            raise OAuthError("empty authorization code")
        # 比照真實 provider：email 未驗證一律拒絕（不回 email、不允許登入）。
        if not self._email_verified:
            raise OAuthError("email not verified")
        return {
            "email": f"{code}@example.com",
            "subject": f"stub-{code}",
            "name": code,
        }


# ── 真實 provider（僅用 stdlib urllib） ───────────────────────────────────────


def _post_form(url: str, data: dict, *, timeout: int = 10) -> dict:
    """POST application/x-www-form-urlencoded，回 JSON dict；失敗包成 OAuthError。"""
    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"OAuth token endpoint HTTP {exc.code}: {exc.reason}") from exc
    except OSError as exc:
        raise OAuthError(f"OAuth token request failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"Unexpected OAuth token error: {exc}") from exc


def _get_json(url: str, *, headers: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise OAuthError(f"OAuth userinfo HTTP {exc.code}: {exc.reason}") from exc
    except OSError as exc:
        raise OAuthError(f"OAuth userinfo request failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"Unexpected OAuth userinfo error: {exc}") from exc


def _email_verified_claim(claims: dict) -> bool:
    """判斷 OIDC/userinfo 的 email_verified 宣告是否為真。

    Google 可能回 boolean True 或字串 "true"（依端點而異）。一律須明確為真，
    缺漏或 false 皆視為未驗證（保守拒絕，防帳號接管）。
    """
    val = claims.get("email_verified")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() == "true"
    return False


def _decode_id_token_claims(id_token: str) -> dict:
    """解碼 OIDC id_token 的 payload claims（不驗章——僅取 email/sub/name）。

    安全說明：email/sub 來自緊接的 token 端點 HTTPS 回應（非使用者直送），
    且 token 由 client_secret 換取；此處僅 base64 解 payload 取宣告值。
    若日後對外暴露更高信任邊界，應改為驗證簽章（需引入相依，本 MVP 避免）。
    """
    try:
        parts = id_token.split(".")
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        return json.loads(decoded)
    except Exception as exc:  # noqa: BLE001
        raise OAuthError("invalid id_token") from exc


class LineLoginProvider(OAuthProvider):
    """真實 LINE Login（OAuth2 + OIDC）。僅用 stdlib urllib。"""

    _AUTHORIZE = "https://access.line.me/oauth2/v2.1/authorize"
    _TOKEN = "https://api.line.me/oauth2/v2.1/token"

    def __init__(
        self, channel_id: str, channel_secret: str, *, timeout: int = 10
    ) -> None:
        self._channel_id = channel_id
        self._channel_secret = channel_secret
        self._timeout = timeout

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        params = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self._channel_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": "openid profile email",
            }
        )
        return f"{self._AUTHORIZE}?{params}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        body = _post_form(
            self._TOKEN,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._channel_id,
                "client_secret": self._channel_secret,
            },
            timeout=self._timeout,
        )
        id_token = body.get("id_token")
        if not id_token:
            raise OAuthError("LINE token response missing id_token")
        claims = _decode_id_token_claims(id_token)
        subject = claims.get("sub")
        if not subject:
            raise OAuthError("LINE id_token missing sub")
        email = claims.get("email")
        # 帳號接管防護：LINE id_token 的 email 僅在帶 email scope 且由 LINE 認證時
        # 出現，視為已驗證；但若 provider 明確回 email_verified==False 仍拒絕。
        if email and claims.get("email_verified") is False:
            email = None
        return {"email": email, "subject": subject, "name": claims.get("name")}


class GoogleOAuthProvider(OAuthProvider):
    """真實 Google OAuth2（OIDC）。僅用 stdlib urllib。"""

    _AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
    _TOKEN = "https://oauth2.googleapis.com/token"
    _USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

    def __init__(
        self, client_id: str, client_secret: str, *, timeout: int = 10
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        params = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": "openid email profile",
            }
        )
        return f"{self._AUTHORIZE}?{params}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        body = _post_form(
            self._TOKEN,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        access_token = body.get("access_token")
        if not access_token:
            raise OAuthError("Google token response missing access_token")
        info = _get_json(
            self._USERINFO,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=self._timeout,
        )
        email = info.get("email")
        subject = info.get("sub")
        if not email or not subject:
            raise OAuthError("Google userinfo missing email/sub")
        # 帳號接管防護：未驗證 email 一律拒絕（不回 email、callback 不登入）。
        # Google 以 email_verified 宣告（boolean，或字串 "true"/"false"）。
        if not _email_verified_claim(info):
            raise OAuthError("Google email not verified")
        return {"email": email, "subject": subject, "name": info.get("name")}


# ── factory（比照 translation.get_translator） ────────────────────────────────


def get_provider(name: str, *, settings, db=None) -> OAuthProvider:
    """依 provider 名稱回傳實例。

    真實 client_id/secret 已設 → 回真實 provider；否則回 StubOAuthProvider
    （離線、決定性、永遠可用），呼叫端無需知道實際 backend。
    """
    if name == "line":
        from saas_mvp.services.platform_oauth_config import effective_line_credentials

        credentials = effective_line_credentials(db, settings)
        if credentials:
            return LineLoginProvider(
                channel_id=credentials[0],
                channel_secret=credentials[1],
            )
        if getattr(settings, "env", "dev") not in ("dev", "test"):
            raise OAuthNotConfigured("LINE Login credentials are not configured")
        return StubOAuthProvider(name="line")
    if name == "google":
        from saas_mvp.services.platform_oauth_config import effective_google_credentials

        credentials = effective_google_credentials(db, settings)
        if credentials:
            return GoogleOAuthProvider(
                client_id=credentials[0],
                client_secret=credentials[1],
            )
        if getattr(settings, "env", "dev") not in ("dev", "test"):
            raise OAuthNotConfigured("Google OAuth credentials are not configured")
        return StubOAuthProvider(name="google")
    raise OAuthError(f"unknown oauth provider: {name!r}")

"""LINE Webhook 管理 client 的官方 HTTP 合約(全程 httpx MockTransport,不連外)。"""

from __future__ import annotations

import json

import httpx
import pytest

from saas_mvp.line_client import (
    HttpLineWebhookAdminClient,
    LineWebhookAdminCredentialError,
    LineWebhookAdminParseError,
)
from tests._line_http import mock_line_http


def _seq_handler(put_body, test_body, get_body, *, captured=None):
    """依 (method, path) 回三段回應;PUT endpoint / POST test / GET endpoint。"""
    def handler(req: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(req)
        path = req.url.path
        if req.method == "PUT" and path.endswith("/webhook/endpoint"):
            return httpx.Response(200, json=put_body)
        if req.method == "POST" and path.endswith("/webhook/test"):
            return httpx.Response(200, json=test_body)
        if req.method == "GET" and path.endswith("/webhook/endpoint"):
            return httpx.Response(200, json=get_body)
        return httpx.Response(500, json={})
    return handler


def test_configure_and_test_calls_put_test_and_get() -> None:
    captured: list[httpx.Request] = []
    handler = _seq_handler(
        {},
        {"success": True, "timestamp": "2026-07-15T00:00:00Z",
         "statusCode": 200, "reason": "OK", "detail": "200"},
        {"endpoint": "https://example.com/line/webhook/7", "active": False},
        captured=captured,
    )
    client = HttpLineWebhookAdminClient(timeout=6)
    with mock_line_http(handler):
        result = client.configure_and_test(
            "https://example.com/line/webhook/7", access_token="secret-token"
        )

    assert result.success is True
    assert result.active is False
    assert result.status_code == 200
    assert [r.method for r in captured] == ["PUT", "POST", "GET"]
    assert all(r.headers["Authorization"] == "Bearer secret-token" for r in captured)
    assert json.loads(captured[0].content) == {
        "endpoint": "https://example.com/line/webhook/7"
    }
    assert json.loads(captured[1].content) == {
        "endpoint": "https://example.com/line/webhook/7"
    }


def test_configure_and_test_preserves_line_failure_diagnostics() -> None:
    handler = _seq_handler(
        {},
        {"success": False, "statusCode": 500, "reason": "ERROR_STATUS_CODE", "detail": "500"},
        {"active": True},
    )
    with mock_line_http(handler):
        result = HttpLineWebhookAdminClient().configure_and_test(
            "https://example.com/line/webhook/7", access_token="token"
        )

    assert result.success is False
    assert result.active is True
    assert result.reason == "ERROR_STATUS_CODE"
    assert result.detail == "500"


def test_configure_and_test_rejects_bad_test_response() -> None:
    handler = _seq_handler({}, {"reason": "OK"}, {"active": True})
    with mock_line_http(handler):
        with pytest.raises(LineWebhookAdminParseError):
            HttpLineWebhookAdminClient().configure_and_test(
                "https://example.com/line/webhook/7", access_token="token"
            )


def test_configure_and_test_maps_credential_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authentication failed"})

    with mock_line_http(handler):
        with pytest.raises(LineWebhookAdminCredentialError):
            HttpLineWebhookAdminClient().configure_and_test(
                "https://example.com/line/webhook/7", access_token="bad-token"
            )

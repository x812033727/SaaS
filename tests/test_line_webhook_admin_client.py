"""LINE Webhook 管理 client 的官方 HTTP 合約（全程 mock，不連外）。"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest import mock

import pytest

from saas_mvp.line_client import (
    HttpLineWebhookAdminClient,
    LineWebhookAdminCredentialError,
    LineWebhookAdminParseError,
)


class _Response:
    def __init__(self, body: dict | str):
        self._body = body if isinstance(body, str) else json.dumps(body)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body.encode()


def test_configure_and_test_calls_put_test_and_get() -> None:
    requests = []
    responses = iter(
        [
            _Response({}),
            _Response(
                {
                    "success": True,
                    "timestamp": "2026-07-15T00:00:00Z",
                    "statusCode": 200,
                    "reason": "OK",
                    "detail": "200",
                }
            ),
            _Response({"endpoint": "https://example.com/line/webhook/7", "active": False}),
        ]
    )

    def _urlopen(req, timeout=None):
        requests.append((req, timeout))
        return next(responses)

    client = HttpLineWebhookAdminClient(timeout=6)
    with mock.patch("urllib.request.urlopen", _urlopen):
        result = client.configure_and_test(
            "https://example.com/line/webhook/7",
            access_token="secret-token",
        )

    assert result.success is True
    assert result.active is False
    assert result.status_code == 200
    assert [request.get_method() for request, _ in requests] == ["PUT", "POST", "GET"]
    assert all(timeout == 6 for _, timeout in requests)
    assert all(
        request.headers["Authorization"] == "Bearer secret-token"
        for request, _ in requests
    )
    assert json.loads(requests[0][0].data) == {
        "endpoint": "https://example.com/line/webhook/7"
    }
    assert json.loads(requests[1][0].data) == {
        "endpoint": "https://example.com/line/webhook/7"
    }


def test_configure_and_test_preserves_line_failure_diagnostics() -> None:
    responses = iter(
        [
            _Response({}),
            _Response(
                {
                    "success": False,
                    "statusCode": 500,
                    "reason": "ERROR_STATUS_CODE",
                    "detail": "500",
                }
            ),
            _Response({"active": True}),
        ]
    )
    with mock.patch("urllib.request.urlopen", lambda *_args, **_kwargs: next(responses)):
        result = HttpLineWebhookAdminClient().configure_and_test(
            "https://example.com/line/webhook/7",
            access_token="token",
        )

    assert result.success is False
    assert result.active is True
    assert result.reason == "ERROR_STATUS_CODE"
    assert result.detail == "500"


def test_configure_and_test_rejects_bad_test_response() -> None:
    responses = iter([_Response({}), _Response({"reason": "OK"})])
    with mock.patch("urllib.request.urlopen", lambda *_args, **_kwargs: next(responses)):
        with pytest.raises(LineWebhookAdminParseError):
            HttpLineWebhookAdminClient().configure_and_test(
                "https://example.com/line/webhook/7",
                access_token="token",
            )


def test_configure_and_test_maps_credential_error() -> None:
    error = urllib.error.HTTPError(
        "https://api.line.me/v2/bot/channel/webhook/endpoint",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b'{"message":"Authentication failed"}'),
    )
    with mock.patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(LineWebhookAdminCredentialError):
            HttpLineWebhookAdminClient().configure_and_test(
                "https://example.com/line/webhook/7",
                access_token="bad-token",
            )

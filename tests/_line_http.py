"""測試用:把 saas_mvp.line_client.http 的池化 client 換成 httpx MockTransport。

R4-P3 後 http.py 走 httpx.Client;測試以此注入回應,不打真實網路。
"""

from __future__ import annotations

import contextlib
from unittest import mock

import httpx

import saas_mvp.line_client.http as line_http


@contextlib.contextmanager
def mock_line_http(handler):
    """handler(httpx.Request) -> httpx.Response;patch _client() 回 MockTransport client。

    handler 可回 httpx.Response,或 raise httpx.RequestError 模擬網路失敗。
    """
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    with mock.patch.object(line_http, "_client", return_value=client):
        yield


def json_response(status: int = 200, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {})


def text_response(status: int, text: str = "") -> httpx.Response:
    return httpx.Response(status, text=text)


def network_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("boom", request=request)

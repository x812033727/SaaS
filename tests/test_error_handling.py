"""集中式未捕捉例外處理：一致 JSON envelope、不外洩內部訊息、錯誤追蹤指標。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from saas_mvp.app import create_app
from saas_mvp.obs import metrics


def _app_with_boom(path: str, exc: Exception):
    app = create_app()

    async def boom():
        raise exc

    app.add_api_route(path, boom, methods=["GET"])
    # raise_server_exceptions=False：模擬真實 ASGI server——用戶端拿到 handler
    # 產生的 JSON 500，而非讓例外往測試端拋（ServerErrorMiddleware 仍會重拋，
    # 此旗標讓 transport 不再往上傳）。
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.REGISTRY.reset()
    yield
    metrics.REGISTRY.reset()


def test_unhandled_exception_returns_json_envelope():
    client = _app_with_boom("/_boom", RuntimeError("super secret internal detail"))
    r = client.get("/_boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["type"] == "InternalServerError"
    assert "request_id" in body["error"] and body["error"]["request_id"]
    # 內部例外訊息 / 類型不得外洩給用戶端
    assert "super secret internal detail" not in r.text
    assert "RuntimeError" not in r.text


def test_error_envelope_request_id_matches_header():
    client = _app_with_boom("/_boom", RuntimeError("x"))
    r = client.get("/_boom")
    rid = r.json()["error"]["request_id"]
    assert r.headers.get("x-request-id") == rid


def test_client_request_id_flows_into_error_envelope():
    client = _app_with_boom("/_boom", RuntimeError("x"))
    r = client.get("/_boom", headers={"X-Request-ID": "trace-err-1"})
    assert r.json()["error"]["request_id"] == "trace-err-1"
    assert r.headers.get("x-request-id") == "trace-err-1"


def test_unhandled_exception_increments_counter_by_type():
    client = _app_with_boom("/_boom_val", ValueError("boom"))
    client.get("/_boom_val")
    rendered = metrics.REGISTRY.render()
    assert "# TYPE http_unhandled_exceptions_total counter" in rendered
    assert 'http_unhandled_exceptions_total{type="ValueError"} 1' in rendered


def test_handled_http_exception_unaffected():
    """既有路由的正常回應 / HTTPException 不受集中式 handler 影響。"""
    client = TestClient(create_app())
    # 既有 404（FastAPI 預設 {"detail": ...}）契約不變
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404
    assert "error" not in r.json()  # 非未捕捉例外，不套 envelope
    assert "detail" in r.json()

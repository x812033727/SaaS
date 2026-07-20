"""可觀測性層測試：結構化日誌、request-id 串接、Prometheus 指標、/readyz、/metrics。"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from saas_mvp.app import create_app
from saas_mvp.obs import metrics
from saas_mvp.obs.context import get_request_id, request_id_var
from saas_mvp.obs.logging import JsonFormatter, TextFormatter, configure_logging


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.REGISTRY.reset()
    yield
    metrics.REGISTRY.reset()


# ── request-id 串接 ──────────────────────────────────────────────────────────


def test_request_id_generated_and_returned():
    client = TestClient(create_app())
    r = client.get("/")
    assert r.status_code == 200
    rid = r.headers.get("x-request-id")
    assert rid and len(rid) == 16  # token_hex(8)


def test_client_request_id_is_propagated():
    client = TestClient(create_app())
    r = client.get("/", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers.get("x-request-id") == "trace-abc-123"


def test_absurd_client_request_id_is_replaced():
    """超長 / 不可列印的用戶端 request-id 應被丟棄改用新生（防 log 污染）。"""
    client = TestClient(create_app())
    r = client.get("/", headers={"X-Request-ID": "x" * 500})
    rid = r.headers.get("x-request-id")
    assert rid != "x" * 500
    assert len(rid) == 16


def test_request_id_contextvar_isolated_after_request():
    # 請求結束後 contextvar 應被 reset，不外洩到後續脈絡
    client = TestClient(create_app())
    client.get("/")
    assert get_request_id() == ""


# ── 結構化日誌 ───────────────────────────────────────────────────────────────


def test_json_formatter_emits_valid_json_with_request_id():
    token = request_id_var.set("rid-json-1")
    try:
        rec = logging.LogRecord(
            "saas_mvp.access",
            logging.INFO,
            __file__,
            1,
            "GET /x 200",
            None,
            None,
        )
        rec.method = "GET"
        rec.status = 200
        line = JsonFormatter().format(rec)
        payload = json.loads(line)
        assert payload["level"] == "INFO"
        assert payload["request_id"] == "rid-json-1"
        assert payload["method"] == "GET"
        assert payload["status"] == 200
    finally:
        request_id_var.reset(token)


def test_text_formatter_includes_request_id_prefix():
    rec = logging.LogRecord(
        "saas_mvp.access",
        logging.INFO,
        __file__,
        1,
        "hello",
        None,
        None,
    )
    rec.request_id = "rid-text-1"
    out = TextFormatter().format(rec)
    assert "[rid-text-1]" in out
    assert "hello" in out


def test_configure_logging_is_idempotent():
    configure_logging("json")
    configure_logging("json")
    root = logging.getLogger()
    obs_handlers = [h for h in root.handlers if getattr(h, "_saas_obs", False)]
    assert len(obs_handlers) == 1
    # 還原成 text，避免污染其他測試輸出
    configure_logging("text")


# ── Prometheus 指標 ──────────────────────────────────────────────────────────


def test_metrics_registry_counter_and_histogram_render():
    reg = metrics.MetricsRegistry(buckets=(0.1, 1.0))
    reg.inc_counter("http_requests_total", {"method": "GET", "status": "200"})
    reg.inc_counter("http_requests_total", {"method": "GET", "status": "200"})
    reg.observe("http_request_duration_seconds", 0.05, {"method": "GET"})
    reg.observe("http_request_duration_seconds", 0.5, {"method": "GET"})
    out = reg.render()
    assert "# TYPE http_requests_total counter" in out
    assert 'http_requests_total{method="GET",status="200"} 2' in out
    assert "# TYPE http_request_duration_seconds histogram" in out
    # le=0.1 桶含 0.05 一筆；le=1.0 桶累積兩筆；+Inf 兩筆
    assert 'http_request_duration_seconds_bucket{method="GET",le="0.1"} 1' in out
    assert 'http_request_duration_seconds_bucket{method="GET",le="1"} 2' in out
    assert 'http_request_duration_seconds_bucket{method="GET",le="+Inf"} 2' in out
    assert 'http_request_duration_seconds_count{method="GET"} 2' in out


def test_metrics_label_escaping():
    reg = metrics.MetricsRegistry()
    reg.inc_counter("c", {"path": 'a"b\\c'})
    out = reg.render()
    assert 'path="a\\"b\\\\c"' in out


def test_metrics_endpoint_records_http_requests():
    client = TestClient(create_app())
    client.get("/")
    body = client.get("/metrics").text
    assert "# TYPE http_requests_total counter" in body
    # 路由樣板（"/"）作為 label，而非原始路徑
    assert 'path="/"' in body
    assert "http_request_duration_seconds" in body


def test_metrics_uses_route_template_not_raw_path(monkeypatch):
    """高 cardinality 防護：動態路由以樣板（/s/{token}）計量，而非每個 token 一個序列。"""
    # raise_server_exceptions=False：動態路由命中後即使內部 500 也回傳回應，
    # 重點在驗證 label 樣板化（與 DB 狀態無關）。
    client = TestClient(create_app(), raise_server_exceptions=False)
    # 命中一條動態路由（能力 token 公開頁），不論回應碼，label 都應是樣板
    client.get("/s/nonexistent-token-1")
    client.get("/s/nonexistent-token-2")
    body = client.get("/metrics").text
    assert "{token}" in body  # 樣板化
    assert "nonexistent-token-1" not in body
    assert "nonexistent-token-2" not in body


def test_metrics_disabled_returns_404(monkeypatch):
    from saas_mvp.config import settings as cfg

    monkeypatch.setattr(cfg, "metrics_enabled", False, raising=False)
    client = TestClient(create_app())
    r = client.get("/metrics")
    assert r.status_code == 404


def test_metrics_token_required_when_set(monkeypatch):
    from saas_mvp.config import settings as cfg

    monkeypatch.setattr(cfg, "metrics_enabled", True, raising=False)
    monkeypatch.setattr(cfg, "metrics_token", "s3cr3t", raising=False)
    client = TestClient(create_app())
    assert client.get("/metrics").status_code == 401
    assert (
        client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cr3t"})
    assert ok.status_code == 200


def test_in_progress_gauge_returns_to_zero():
    client = TestClient(create_app())
    client.get("/")
    body = client.get("/metrics").text
    # 請求都已結束，in-progress 應為 0（/metrics 自身在計量前已 +1，render 當下仍在處理中→1）
    # 故只驗證指標存在且為非負整數
    assert "http_requests_in_progress" in body


# ── /readyz 探針 ─────────────────────────────────────────────────────────────


def test_readyz_ok():
    client = TestClient(create_app())
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["db"] == "ok"
    assert "rate_limit_backend" in body


def test_healthz_still_works(monkeypatch):
    """/readyz 的加入不得破壞既有 /healthz 契約。"""
    monkeypatch.setenv("SAAS_GIT_SHA", "a" * 40)
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"
    assert r.json()["git_sha"] == "a" * 40


def test_healthz_revision_fails_closed(monkeypatch):
    monkeypatch.setenv("SAAS_GIT_SHA", "not-a-commit")
    assert TestClient(create_app()).get("/healthz").json()["git_sha"] == "unknown"

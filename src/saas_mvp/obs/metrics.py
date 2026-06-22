"""極簡、零相依的 Prometheus 文字曝露 (text exposition) 計量器。

刻意不引入 ``prometheus_client``（保持 pyproject 釘版精簡、避免重依賴）。
僅實作 SaaS 上線所需的三種型別：counter / gauge / histogram，
並輸出標準 Prometheus 0.0.4 text 格式供 ``/metrics`` scrape。

多 worker 提醒：本 registry 為 **per-process**（每個 gunicorn worker 一份）。
scrape 時各 worker 各自回報；要全域聚合請在 Prometheus 端 ``sum()``，
或讓 LB 對 ``/metrics`` 採 sticky/逐 worker 抓取。詳見 README 部署章節。
"""

from __future__ import annotations

import threading
from typing import Iterable

# 預設 latency 桶（秒）；涵蓋 5ms～10s，足夠涵蓋 web 請求分佈。
_DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

# label 值轉義（Prometheus text 格式要求 \ " \n 轉義）
def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(key: tuple[tuple[str, str], ...], extra: tuple[str, str] | None = None) -> str:
    parts = [f'{k}="{_escape(v)}"' for k, v in key]
    if extra is not None:
        parts.append(f'{extra[0]}="{_escape(extra[1])}"')
    return "{" + ",".join(parts) + "}" if parts else ""


class MetricsRegistry:
    """執行緒安全的 in-process 計量器集合。"""

    def __init__(self, buckets: Iterable[float] = _DEFAULT_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._buckets = tuple(sorted(buckets))
        # name -> {labels_key -> value}
        self._counters: dict[str, dict[tuple, float]] = {}
        self._gauges: dict[str, dict[tuple, float]] = {}
        # histogram: name -> {labels_key -> {"buckets":[...], "sum":float, "count":int}}
        self._hist: dict[str, dict[tuple, dict]] = {}
        self._help: dict[str, str] = {}

    def _ensure_help(self, name: str, help_text: str) -> None:
        self._help.setdefault(name, help_text)

    def inc_counter(self, name: str, labels: dict[str, str] | None = None,
                    value: float = 1.0, help_text: str = "") -> None:
        key = _labels_key(labels or {})
        with self._lock:
            self._ensure_help(name, help_text)
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None,
                  help_text: str = "") -> None:
        key = _labels_key(labels or {})
        with self._lock:
            self._ensure_help(name, help_text)
            self._gauges.setdefault(name, {})[key] = value

    def inc_gauge(self, name: str, delta: float = 1.0, labels: dict[str, str] | None = None,
                  help_text: str = "") -> None:
        key = _labels_key(labels or {})
        with self._lock:
            self._ensure_help(name, help_text)
            series = self._gauges.setdefault(name, {})
            series[key] = series.get(key, 0.0) + delta

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None,
                help_text: str = "") -> None:
        key = _labels_key(labels or {})
        with self._lock:
            self._ensure_help(name, help_text)
            series = self._hist.setdefault(name, {})
            entry = series.get(key)
            if entry is None:
                entry = {"buckets": [0] * len(self._buckets), "sum": 0.0, "count": 0}
                series[key] = entry
            entry["sum"] += value
            entry["count"] += 1
            # 只計入最小符合的桶（非累積）；render 時再做 cumulative 累加，
            # 避免「observe 累積 + render 累積」雙重計數。
            for i, ub in enumerate(self._buckets):
                if value <= ub:
                    entry["buckets"][i] += 1
                    break

    def render(self) -> str:
        """輸出 Prometheus 0.0.4 text exposition 格式。"""
        lines: list[str] = []
        with self._lock:
            for name in sorted(set(self._counters) | set(self._gauges) | set(self._hist)):
                help_text = self._help.get(name, "")
                if name in self._counters:
                    if help_text:
                        lines.append(f"# HELP {name} {help_text}")
                    lines.append(f"# TYPE {name} counter")
                    for key, val in sorted(self._counters[name].items()):
                        lines.append(f"{name}{_render_labels(key)} {_fmt(val)}")
                if name in self._gauges:
                    if help_text:
                        lines.append(f"# HELP {name} {help_text}")
                    lines.append(f"# TYPE {name} gauge")
                    for key, val in sorted(self._gauges[name].items()):
                        lines.append(f"{name}{_render_labels(key)} {_fmt(val)}")
                if name in self._hist:
                    if help_text:
                        lines.append(f"# HELP {name} {help_text}")
                    lines.append(f"# TYPE {name} histogram")
                    for key, entry in sorted(self._hist[name].items()):
                        cumulative = 0
                        for i, ub in enumerate(self._buckets):
                            cumulative += entry["buckets"][i]
                            le = _fmt(ub)
                            lines.append(
                                f'{name}_bucket{_render_labels(key, ("le", le))} {cumulative}'
                            )
                        lines.append(
                            f'{name}_bucket{_render_labels(key, ("le", "+Inf"))} {entry["count"]}'
                        )
                        lines.append(f"{name}_sum{_render_labels(key)} {_fmt(entry['sum'])}")
                        lines.append(f"{name}_count{_render_labels(key)} {entry['count']}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """清空所有序列（測試用）。"""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist.clear()
            self._help.clear()


def _fmt(value: float) -> str:
    """Prometheus 數值格式：整數去小數點，其餘用 repr 保留精度。"""
    if value == int(value):
        return str(int(value))
    return repr(value)


# 模組級單例（per-process）。
REGISTRY = MetricsRegistry()

# 對外指標常數名稱（集中管理，避免打錯字）。
HTTP_REQUESTS_TOTAL = "http_requests_total"
HTTP_REQUEST_DURATION = "http_request_duration_seconds"
HTTP_IN_PROGRESS = "http_requests_in_progress"

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

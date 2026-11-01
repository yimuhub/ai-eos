"""轻量 Prometheus 指标：RED + LLM 调用 + 任务队列。

## 为什么手写而不是引入 prometheus_client

运行环境没有 ``prometheus_client``，而可观测性不该以"新增一个联网依赖"
为代价。Prometheus 的文本暴露格式本身很小：``# HELP`` / ``# TYPE`` +
样本行，标签只需转义反斜杠、双引号与换行。这里用标准库实现
Counter / Gauge / Histogram 三个原语，语义与用法向 prometheus_client
对齐（``inc`` / ``set`` / ``observe`` + labels），后续若要换库，
调用方无需改动。

## 低基数（low cardinality）是硬约束

Prometheus 的每条标签组合都是一个时间序列。把真实任务 id、agent id、
完整 URL 当标签，几小时就能撑爆内存与抓取端。因此：

* HTTP 的 ``path`` 来自**路由模板**（``/agents/{aid}``），未匹配路由统一
  记为 ``unmatched``；
* 方法、状态码都是有限集合；
* provider / model 来自模型配置，而不是每次请求的 id；
* 任务 kind 收敛到 ``task`` / ``meeting`` / ``workflow`` / ``orchestration``
  / ``chat`` / ``job``。

## 线程安全

HTTP 中间件在事件循环里调用，任务执行器在 worker 线程里调用，LLM 网关
在异步协程里调用。所有读写都在 ``threading.Lock`` 保护下完成；单次操作
只持锁做字典读写，不会在锁内做 IO。
"""
from __future__ import annotations

import threading
from typing import Iterable, Mapping, Sequence

# 默认直方图桶（秒）。覆盖从缓存命中到慢模型/长任务的常见量级。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
)


def _escape_label_value(value: object) -> str:
    """按 Prometheus 文本格式转义标签值。

    规范只要求转义反斜杠、双引号和换行；其余字符（含中文）可以原样输出。
    """
    text = str(value)
    return (text.replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace('"', '\\"'))


def _format_value(value: float | int) -> str:
    """整数尽量输出整数形式，浮点用最短可读表示。"""
    if isinstance(value, int):
        return str(value)
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


def _format_labels(labelnames: Sequence[str], key: tuple[str, ...]) -> str:
    if not labelnames:
        return ""
    parts = [f'{name}="{_escape_label_value(value)}"'
             for name, value in zip(labelnames, key)]
    return "{" + ",".join(parts) + "}"


def _labels_key(labelnames: Sequence[str], labels: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(labels.get(name, "")) for name in labelnames)


class _Metric:
    """所有指标共享的名字/帮助文本/标签定义与渲染骨架。"""

    type_name = ""

    def __init__(self, name: str, help_text: str,
                 labelnames: Sequence[str] = ()) -> None:
        if not name:
            raise ValueError("metric name must not be empty")
        if "\n" in help_text:
            # HELP 是单行文本；换行会破坏整个抓取响应。
            help_text = help_text.replace("\n", " ")
        self.name = name
        self.help_text = help_text
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()

    def _header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help_text}",
                f"# TYPE {self.name} {self.type_name}"]

    def _render_locked(self) -> list[str]:  # pragma: no cover - 抽象
        raise NotImplementedError


class Counter(_Metric):
    """单调递增计数器。"""

    type_name = "counter"

    def __init__(self, name: str, help_text: str,
                 labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, value: float = 1, **labels: object) -> None:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(value)

    def get(self, **labels: object) -> float:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def _render_locked(self) -> list[str]:
        lines = self._header()
        for key in sorted(self._values):
            lines.append(f"{self.name}{_format_labels(self.labelnames, key)} "
                         f"{_format_value(self._values[key])}")
        return lines


class Gauge(_Metric):
    """可升可降的瞬时值。"""

    type_name = "gauge"

    def __init__(self, name: str, help_text: str,
                 labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        # 无标签的 gauge（如 in-flight）先落一个 0 样本，保证抓取端
        # 在第一次请求之前也能看到它，而不是以为指标不存在。
        self._values: dict[tuple[str, ...], float] = (
            {} if labelnames else {(): 0.0})

    def set(self, value: float, **labels: object) -> None:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, value: float = 1, **labels: object) -> None:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(value)

    def dec(self, value: float = 1, **labels: object) -> None:
        self.inc(-float(value), **labels)

    def get(self, **labels: object) -> float:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def _render_locked(self) -> list[str]:
        lines = self._header()
        for key in sorted(self._values):
            lines.append(f"{self.name}{_format_labels(self.labelnames, key)} "
                         f"{_format_value(self._values[key])}")
        return lines


class Histogram(_Metric):
    """累积直方图：``_bucket{le=...}`` / ``_sum`` / ``_count``。"""

    type_name = "histogram"

    def __init__(self, name: str, help_text: str,
                 labelnames: Sequence[str] = (),
                 buckets: Iterable[float] = DEFAULT_BUCKETS) -> None:
        super().__init__(name, help_text, labelnames)
        # 去重并排序，保证渲染顺序稳定、le 单调递增。
        self.buckets = tuple(sorted({float(b) for b in buckets}))
        self._values: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._counts: dict[tuple[str, ...], int] = {}

    def observe(self, value: float, **labels: object) -> None:
        number = float(value)
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            bucket_counts = self._values.get(key)
            if bucket_counts is None:
                bucket_counts = [0] * len(self.buckets)
                self._values[key] = bucket_counts
            for index, bound in enumerate(self.buckets):
                if number <= bound:
                    bucket_counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + number
            self._counts[key] = self._counts.get(key, 0) + 1

    def count(self, **labels: object) -> int:
        key = _labels_key(self.labelnames, labels)
        with self._lock:
            return self._counts.get(key, 0)

    def _render_locked(self) -> list[str]:
        lines = self._header()
        for key in sorted(self._values):
            labels = _format_labels(self.labelnames, key)
            for bound, count in zip(self.buckets, self._values[key]):
                extra = _format_labels(("le",), (_format_value(bound),))
                # 有其它标签时把 le 拼进去，没有时用单纯的 {le="..."}。
                line_labels = (labels[:-1] + "," + extra[1:]
                               if labels else extra)
                lines.append(f"{self.name}_bucket{line_labels} {count}")
            total = self._counts.get(key, 0)
            infinite = _format_labels(("le",), ("+Inf",))
            line_labels = (labels[:-1] + "," + infinite[1:]
                           if labels else infinite)
            lines.append(f"{self.name}_bucket{line_labels} {total}")
            lines.append(f"{self.name}_sum{labels} "
                         f"{_format_value(self._sums.get(key, 0.0))}")
            lines.append(f"{self.name}_count{labels} {total}")
        return lines


# ---------------------------------------------------------------------------
# 预定义指标（低基数标签）
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "aieos_http_requests_total",
    "HTTP requests handled, by method, route template and status code.",
    ("method", "path", "status"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "aieos_http_request_duration_seconds",
    "HTTP request duration in seconds, by method and route template.",
    ("method", "path"),
)

HTTP_REQUESTS_IN_FLIGHT = Gauge(
    "aieos_http_requests_in_flight",
    "HTTP requests currently being handled.",
)

LLM_REQUESTS_TOTAL = Counter(
    "aieos_llm_requests_total",
    "LLM provider calls, by provider, model and outcome.",
    ("provider", "model", "outcome"),
)

LLM_DURATION_SECONDS = Histogram(
    "aieos_llm_duration_seconds",
    "LLM provider call duration in seconds, by provider and model.",
    ("provider", "model"),
)

LLM_TOKENS_TOTAL = Counter(
    "aieos_llm_tokens_total",
    "LLM tokens consumed, by provider, model and kind (input/output).",
    ("provider", "model", "kind"),
)

TASK_QUEUE_DEPTH = Gauge(
    "aieos_task_queue_depth",
    "Background jobs waiting (kind=pending) or executing (kind=running).",
    ("kind",),
)

TASK_DURATION_SECONDS = Histogram(
    "aieos_task_duration_seconds",
    "Background job duration in seconds, by task kind and outcome.",
    ("kind", "outcome"),
)

# 队列为空时也暴露 0，而不是等第一单任务才出现指标。
TASK_QUEUE_DEPTH.set(0, kind="pending")
TASK_QUEUE_DEPTH.set(0, kind="running")

_ALL_METRICS: tuple[_Metric, ...] = (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_FLIGHT,
    LLM_REQUESTS_TOTAL,
    LLM_DURATION_SECONDS,
    LLM_TOKENS_TOTAL,
    TASK_QUEUE_DEPTH,
    TASK_DURATION_SECONDS,
)


def render_prometheus(metrics: Iterable[_Metric] | None = None) -> str:
    """渲染 Prometheus 文本暴露格式。

    ``metrics`` 仅供单元测试隔离使用；生产调用一律 ``render_prometheus()``。
    返回值以换行结尾，可直接作为 ``text/plain; version=0.0.4`` 响应体。
    """
    registry = _ALL_METRICS if metrics is None else tuple(metrics)
    lines: list[str] = []
    for metric in registry:
        with metric._lock:
            lines.extend(metric._render_locked())
        lines.append("")
    return "\n".join(lines) + ("\n" if lines else "")

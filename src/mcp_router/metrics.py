"""Prometheus-метрики: вызовы tools, статусы серверов, HTTP-метрики.

Используется официальная библиотека ``prometheus-client``. Эндпоинт ``/metrics``
отдаётся без аутентификации (доступен внутри docker-сети).

Сбор вызовов tools встроен в ``DownstreamClient.call_tool`` — единую точку,
через которую проходят и прямой вызов ``/run/tool/call``, и MCP ``tools/call``
на ``/mcp``. Таким образом метрика покрывает оба пути и фиксирует статус
(ok/error) по ответу нижестоящего MCP-сервера.
"""

from __future__ import annotations

import time

from prometheus_client import Counter, Gauge, Histogram, generate_latest
from prometheus_client.registry import REGISTRY
from starlette.responses import Response

# --- вызовы tools ---
TOOL_CALLS = Counter(
    "mcp_router_tool_calls_total",
    "Всего вызовов tools по результату (ok/error)",
    ["server", "tool", "status"],
)
TOOL_CALL_DURATION = Histogram(
    "mcp_router_tool_calls_duration_seconds",
    "Длительность вызовов tools",
    ["server", "tool"],
)

# --- статусы серверов ---
SERVER_UP = Gauge(
    "mcp_router_server_up",
    "Доступен ли сервер: 1 (connected) или 0",
    ["server"],
)
SERVER_STATUS = Gauge(
    "mcp_router_server_status",
    "Статус сервера: 1=connected, 2=disconnected, 3=error",
    ["server"],
)
SERVER_TOOLS = Gauge(
    "mcp_router_server_tools_total",
    "Число зарегистрированных tools сервера",
    ["server"],
)

# --- HTTP ---
HTTP_REQUESTS = Counter(
    "mcp_router_http_requests_total",
    "HTTP-запросы по method/path/status (path нормализован до шаблона)",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "mcp_router_http_request_duration_seconds",
    "Длительность HTTP-запросов (до первых байт ответа)",
    ["method", "path"],
)

_STATUS_MAP = {
    "connected": 1,
    "disconnected": 2,
    "error": 3,
}


def record_tool_call(server: str, tool: str, duration: float, is_error: bool) -> None:
    """Фиксирует вызов tool с результатом ok/error и длительностью."""
    status = "error" if is_error else "ok"
    TOOL_CALLS.labels(server=server, tool=tool, status=status).inc()
    TOOL_CALL_DURATION.labels(server=server, tool=tool).observe(duration)


def set_server_status(server: str, status_value: str) -> None:
    """Обновляет gauge статуса сервера по строковому значению (ServerStatus.value)."""
    SERVER_STATUS.labels(server=server).set(_STATUS_MAP[status_value])
    SERVER_UP.labels(server=server).set(1 if status_value == "connected" else 0)


def set_server_tools(server: str, count: int) -> None:
    SERVER_TOOLS.labels(server=server).set(count)


def normalize_path(path: str) -> str:
    """Сводит динамические пути к шаблону, чтобы не раздувать cardinality."""
    if path.startswith("/mcp"):
        return "/mcp"
    if path == "/run/select" or path == "/run/tool/call":
        return path
    if path.startswith("/run/select/"):
        return "/run/select/{group}"
    if path == "/metrics":
        return "/metrics"
    if path.startswith("/api/tool/"):
        return "/api/tool/{tool}"
    segs = []
    for s in path.strip("/").split("/"):
        segs.append("{n}" if s.isdigit() else s)
    joined = "/".join(segs)
    return "/" + joined if joined else "/"


class MetricsMiddleware:
    """Чистый ASGI-middleware: HTTP-метрики без буферизации (не ломает SSE)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.monotonic()
        method = scope.get("method", "")

        async def _send(message):
            if message["type"] == "http.response.start":
                status = message["status"]
                path = normalize_path(scope.get("path", ""))
                HTTP_REQUESTS.labels(method=method, path=path, status=status).inc()
                HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(
                    time.monotonic() - start
                )
            await send(message)

        await self.app(scope, receive, _send)


def metrics_endpoint() -> Response:
    """Возвращает выгрузку метрик в формате Prometheus text exposition."""
    return Response(
        generate_latest(REGISTRY),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

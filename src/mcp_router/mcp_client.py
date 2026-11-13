"""MCP-клиент: подключение к нижестоящим MCP-серверам.

Обеспечивает агрегатор подключений (по одному долгоживущему ``Client`` на сервер),
circuit breaker и таймауты. Обрывы SSE-стримов SDK переживает сам (авто-reconnect);
при недоступности сервера требуется вмешательство оператора.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog
from mcp.client import Client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult
from mcp_types import Tool as MCPTool

from .config import ServerConfig
from .metrics import record_tool_call
from .registry import Registry, ServerStatus

logger = structlog.get_logger()

# Read timeout для долгоживущего GET-стрима (server-initiated события).
# Равен рекомендации SDK (MCP_DEFAULT_SSE_READ_TIMEOUT): 300s, т.к. сервер
# может долго держать стрим открытым без событий. Использование короткого
# read timeout (например server.timeout=30) заставляло httpx закрывать стрим
# каждые 30s, вызывая бесконечные реконнекты.
SSE_READ_TIMEOUT = 300.0


def is_sse_url(url: str) -> bool:
    """Определяет транспорт по URL: SSE, если путь оканчивается на /sse, иначе streamable HTTP."""
    return url.rstrip("/").endswith("/sse")


def mask_url(url: str) -> str:
    """Маскирует секреты (query-параметры, токены) в URL перед логированием."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return url


class CircuitBreaker:
    """Простой circuit breaker: open после N ошибок в окне, recovery через таймаут."""

    def __init__(self, threshold: int = 5, window: float = 60.0, recovery: float = 30.0) -> None:
        self.threshold = threshold
        self.window = window
        self.recovery = recovery
        self._failures: list[float] = []
        self._opened_at: float | None = None
        self._closed = True

    @property
    def is_open(self) -> bool:
        if not self._closed:
            if time.monotonic() - self._opened_at >= self.recovery:
                self._closed = True  # half-open: allow one probe
                self._failures = []
                return False
            return True
        return False

    def allow_request(self) -> bool:
        return not self.is_open

    def record_success(self) -> None:
        self._closed = True
        self._failures = []
        self._opened_at = None

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        self._failures = [f for f in self._failures if now - f <= self.window]
        if len(self._failures) >= self.threshold:
            self._closed = False
            self._opened_at = now


class DownstreamClient:
    """Клиент к одному нижестоящему MCP-серверу с долгоживущей сессией."""

    def __init__(self, server: ServerConfig, headers: dict[str, str]) -> None:
        self.server = server
        self.headers = headers
        cb = server.circuit_breaker
        self.breaker = CircuitBreaker(
            threshold=cb.threshold, window=cb.window, recovery=cb.recovery
        )
        self._client: Client | None = None
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def connect(self) -> None:
        """Устанавливает долгоживущее подключение к серверу."""
        async with self._lock:
            if self._client is not None:
                return
            transport = self._build_transport()
            self._client = Client(transport, read_timeout_seconds=self.server.timeout)
            try:
                await self._client.__aenter__()
            except Exception:
                self._client = None
                await self._close_http()
                raise

    def _build_transport(self):
        if is_sse_url(self.server.url):
            return sse_client(self.server.url, headers=self.headers)
        self._http = httpx.AsyncClient(
            base_url=self.server.url,
            headers=self.headers,
            timeout=httpx.Timeout(self.server.timeout, read=SSE_READ_TIMEOUT),
        )
        return streamable_http_client(self.server.url, http_client=self._http)

    @property
    def status(self) -> ServerStatus:
        if self.breaker.is_open:
            return ServerStatus.ERROR
        return ServerStatus.CONNECTED if self._client is not None else ServerStatus.DISCONNECTED

    async def list_tools(self) -> list[MCPTool]:
        client = self._require_client()
        try:
            result = await client.list_tools()
            self.breaker.record_success()
            return result.tools
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure()
            self.last_error = str(exc)
            raise

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        client = self._require_client()
        start = time.monotonic()
        try:
            result = await client.call_tool(name, arguments)
            self.breaker.record_success()
            is_error = bool(getattr(result, "is_error", False))
            record_tool_call(self.server.name, name, time.monotonic() - start, is_error)
            return result
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure()
            self.last_error = str(exc)
            record_tool_call(self.server.name, name, time.monotonic() - start, True)
            raise

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError(f"Сервер '{self.server.name}' не подключён")
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                pass
            self._client = None
        await self._close_http()

    async def _close_http(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                pass
            self._http = None


class ClientPool:
    """Агрегатор подключений ко всем серверам + фоновый health check."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self._clients: dict[str, DownstreamClient] = {}
        self._health_task: asyncio.Task | None = None
        self._health_interval = 30.0

    def client(self, server_name: str) -> DownstreamClient | None:
        return self._clients.get(server_name)

    async def start(self) -> None:
        for server in self.registry.enabled_servers():
            headers = server.header_map
            dc = DownstreamClient(server, headers)
            self._clients[server.name] = dc
            try:
                await dc.connect()
                self.registry.set_status(server.name, ServerStatus.CONNECTED)
            except Exception as exc:  # noqa: BLE001
                self.registry.set_status(server.name, ServerStatus.ERROR)
                logger.warning(
                    "server.connect_failed",
                    server=server.name,
                    url=mask_url(server.url),
                    error=str(exc),
                )
        self._health_task = asyncio.create_task(self._health_loop())

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._health_interval)
            for name, dc in list(self._clients.items()):
                # Активных сетевых проверок не делаем: статус берём из текущего
                # состояния подключения (dc.status), которое обновляется по факту
                # операций (list_tools/call_tool) через circuit breaker и по
                # живости transport/_client. Обрыв GET-стрима SDK переживает сам
                # (авто-reconnect), поэтому он не меняет статус, пока соединение живо.
                status = dc.status
                self.registry.set_status(name, status)
                logger.info("server.health", server=name, status=status.value)

    def is_available(self, server_name: str) -> bool:
        dc = self._clients.get(server_name)
        if dc is None:
            return False
        return dc.status == ServerStatus.CONNECTED and dc.breaker.allow_request()

    async def refresh_server(self, server_name: str) -> None:
        """Переподключается к серверу и перечитывает список его tools."""
        dc = self._clients.get(server_name)
        if dc is None:
            raise KeyError(server_name)
        await dc.close()
        await dc.connect()
        tools = await dc.list_tools()
        return tools

    async def call_tool(self, qualified: str, arguments: dict[str, Any]) -> CallToolResult:
        server_name, tool_name = qualified.split(".", 1)
        dc = self._clients.get(server_name)
        if dc is None:
            raise ValueError(f"Неизвестный сервер: {server_name}")
        if not dc.breaker.allow_request():
            raise RuntimeError(f"Сервер '{server_name}' недоступен (circuit breaker open)")
        return await dc.call_tool(tool_name, arguments)

    async def shutdown(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                pass
        for client in list(self._clients.values()):
            try:
                await client.close()
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                pass
        self._clients.clear()

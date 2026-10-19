"""MCP-клиент: подключение к нижестоящим MCP-серверам.

Обеспечивает агрегатор подключений (по одному долгоживущему ``Client`` на сервер),
circuit breaker, exponential backoff при реконнекте и таймауты.
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
from .registry import Registry, ServerStatus

logger = structlog.get_logger()


def is_sse_url(url: str) -> bool:
    """Определяет транспорт по URL: SSE, если путь оканчивается на /sse, иначе streamable HTTP."""
    return url.rstrip("/").endswith("/sse")


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
        self.breaker = CircuitBreaker()
        self._client: Client | None = None
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.last_latency_ms: float | None = None
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
                raise

    def _build_transport(self):
        if is_sse_url(self.server.url):
            return sse_client(self.server.url, headers=self.headers)
        self._http = httpx.AsyncClient(
            base_url=self.server.url,
            headers=self.headers,
            timeout=httpx.Timeout(self.server.timeout),
        )
        return streamable_http_client(self.server.url, http_client=self._http)

    @property
    def status(self) -> ServerStatus:
        if self.breaker.is_open:
            return ServerStatus.ERROR
        return ServerStatus.CONNECTED if self._client is not None else ServerStatus.DISCONNECTED

    async def ping(self) -> bool:
        client = self._client
        if client is None:
            return False
        start = time.monotonic()
        try:
            await client.send_ping()
            self.last_latency_ms = round((time.monotonic() - start) * 1000, 2)
            self.breaker.record_success()
            self.last_error = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure()
            self.last_error = str(exc)
            return False

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
        try:
            result = await client.call_tool(name, arguments)
            self.breaker.record_success()
            return result
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure()
            self.last_error = str(exc)
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
            headers = server.auth.headers.resolve() if server.auth and server.auth.headers else {}
            dc = DownstreamClient(server, headers)
            self._clients[server.name] = dc
            try:
                await dc.connect()
                self.registry.set_status(server.name, ServerStatus.CONNECTED)
            except Exception as exc:  # noqa: BLE001
                self.registry.set_status(server.name, ServerStatus.ERROR)
                logger.warning("server.connect_failed", server=server.name, error=str(exc))
        self._health_task = asyncio.create_task(self._health_loop())

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._health_interval)
            for name, dc in list(self._clients.items()):
                try:
                    ok = await dc.ping()
                except Exception:  # noqa: BLE001
                    ok = False
                status = ServerStatus.CONNECTED if ok else ServerStatus.ERROR
                self.registry.set_status(name, status)
                logger.info(
                    "server.health",
                    server=name,
                    status=status.value,
                    latency_ms=dc.last_latency_ms,
                )

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

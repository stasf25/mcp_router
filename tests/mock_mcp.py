"""Утилиты для тестов: mock нижестоящего MCP-сервера."""

from __future__ import annotations

import json
import socket
import threading

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context


def build_mock_server(name: str = "mock") -> MCPServer:
    """Создаёт in-process MCP-сервер с несколькими tools для тестов."""
    server = MCPServer(name=name)

    @server.tool(name="navigate", description="Открыть URL в браузере")
    async def navigate(url: str) -> str:
        return f"navigated:{url}"

    @server.tool(name="click", description="Кликнуть по селектору")
    async def click(selector: str, timeout: int = 1000) -> str:
        return f"clicked:{selector}:{timeout}"

    @server.tool(name="screenshot", description="Сделать скриншот")
    async def screenshot(path: str | None = None) -> str:
        return f"screenshot:{path}"

    return server


class FailSwitch:
    """Флаг, переключающий mock-сервер в режим «все вызовы падают»."""

    def __init__(self) -> None:
        self.fail = False


def build_flaky_mock_server(name: str = "flaky", switch: FailSwitch | None = None) -> MCPServer:
    """Mock-сервер, чей tool можно заставить падать через ``switch.fail``.

    Позволяет эмулировать недоступность нижестоящего сервера в runtime
    (для интеграционного теста circuit breaker / graceful degradation).
    """
    switch = switch or FailSwitch()
    server = MCPServer(name=name)

    @server.tool(name="ping", description="Проверка доступности")
    async def ping() -> str:
        if switch.fail:
            raise RuntimeError("mock server unavailable")
        return "pong"

    return server


def build_meta_capture_mock_server(name: str = "capture") -> MCPServer:
    """Mock-сервер с tool, который возвращает `_meta` входящего запроса.

    Позволяет проверить, что роутер пробрасывает `session_id` нижестоящему
    серверу через ключ в `_meta` (вариант A, mcp_router_TZ_sessions.md).
    `context.request_context.meta` — это `_meta` входящего tools/call.
    """
    server = MCPServer(name=name)

    @server.tool(name="echo_meta", description="Возвращает _meta запроса")
    async def echo_meta(context: Context) -> str:
        meta = getattr(context.request_context, "meta", None)
        return json.dumps(meta or {}, sort_keys=True)

    return server


def serve_mcp_server_on_thread(app, port: int, path: str = "/mcp") -> uvicorn.Server:
    """Запускает Starlette/MCPServer app на указанном порту в фоновом потоке."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    return uvicorn_server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

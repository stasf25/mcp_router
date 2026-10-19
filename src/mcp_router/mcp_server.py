"""Единый MCP-сервер наружу: ScopedMCPServer на `/mcp`.

Подкласс ``MCPServer`` (mcp==2.3.0). ``FilterSpec`` вычисляется на каждый запрос
из пути (агрегатор / группа / один tool), что автоматически отражает текущее
состояние реестра (availability серверов, группы, JWT).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult
from mcp_types import Tool as MCPTool
from starlette.applications import Starlette

from .mcp_client import ClientPool
from .registry import FilterSpec, Registry

MCP_MOUNT = "/mcp"

RESOLVED_ERROR = "Tool {name} not in scope"


@dataclass
class RequestCtx:
    path: str
    headers: dict[str, str]


_REQUEST_CTX: ContextVar[RequestCtx | None] = ContextVar("mcp_request_ctx", default=None)


def current_request() -> RequestCtx | None:
    return _REQUEST_CTX.get()


def _spec_from_path(path: str) -> FilterSpec:
    if path.startswith(MCP_MOUNT):
        rest = path[len(MCP_MOUNT):].strip("/")
    else:
        rest = path.strip("/")
    if not rest:
        return FilterSpec.all()
    if "." in rest:
        return FilterSpec.single(rest)
    return FilterSpec.for_group(rest)


class ScopedMCPServer(MCPServer):
    """MCP-сервер, отдающий tools согласно FilterSpec из пути запроса."""

    def __init__(self, registry: Registry, pool: ClientPool, name: str = "mcp_router") -> None:
        super().__init__(name=name)
        self._registry = registry
        self._pool = pool

    def _resolve_spec(self, context=None) -> FilterSpec:
        if context is not None:
            path = context.request_context.request.url.path
        else:
            ctx = _REQUEST_CTX.get()
            path = ctx.path if ctx else ""
        return _spec_from_path(path)

    async def list_tools(self) -> list[MCPTool]:
        spec = self._resolve_spec()
        return self._registry.mcp_tools(spec)

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None) -> CallToolResult:
        spec = self._resolve_spec(context)
        if not self._registry.is_tool_available(name, spec):
            raise ToolNotAvailableError(RESOLVED_ERROR.format(name=name))
        return await self._pool.call_tool(name, arguments)


class ToolNotAvailableError(Exception):
    """Tool не входит в скоуп данного endpoint."""


class CaptureContextMiddleware:
    """Чистый ASGI-middleware: фиксирует path/headers в contextvar, ответ не буферизует.

    BaseHTTPMiddleware буферизует streaming-ответы (ломает SSE MCP), поэтому
    используем низкоуровневый ASGI-пропуск.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            token = _REQUEST_CTX.set(RequestCtx(path=scope["path"], headers=headers))
            try:
                await self.app(scope, receive, send)
            finally:
                _REQUEST_CTX.reset(token)
        else:
            await self.app(scope, receive, send)


def build_mcp_app(server: ScopedMCPServer) -> Starlette:
    """Возвращает streamable_http_app сервера с ASGI-middleware захвата контекста.

    Используется wildcard-путь ``/{rest:path}``, чтобы один app обслуживал
    все подпути /mcp, /mcp/{group}, /mcp/{server}.{tool}. Middleware добавлен
    напрямую в inner-app (без обёртки), чтобы не потерять его lifespan,
    который инициализирует session manager (иначе 500 "Task group not initialized").
    """
    inner = server.streamable_http_app(streamable_http_path="/{rest:path}")
    inner.add_middleware(CaptureContextMiddleware)
    return inner

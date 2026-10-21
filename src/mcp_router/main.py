"""Точка входа FastAPI-приложения mcp_router.

Загружает config.yaml, строит AppState (registry + pool), монтирует
единый MCP-endpoint на /mcp, подключает REST API (/api) и админку (/).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI

from . import admin, api, run_api
from .config import load_config
from .mcp_server import MCP_MOUNT, ScopedMCPServer, build_mcp_app
from .registry import ServerStatus, ToolInfo
from .state import AppState

logger = structlog.get_logger()


async def discover_tools(state: AppState) -> None:
    """Подключает серверы и наполняет registry списками их tools."""
    await state.pool.start()
    for server in state.registry.enabled_servers():
        dc = state.pool.client(server.name)
        if dc is None:
            continue
        try:
            tools = await dc.list_tools()
            for t in tools:
                state.registry.register_tool(ToolInfo(server=server.name, name=t.name, schema=t))
            state.registry.set_status(server.name, ServerStatus.CONNECTED)
            logger.info("server.tools_discovered", server=server.name, count=len(tools))
        except Exception as exc:  # noqa: BLE001
            state.registry.set_status(server.name, ServerStatus.ERROR)
            logger.warning("server.discover_failed", server=server.name, error=str(exc))


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
    state = AppState.build(config)

    mcp_server = ScopedMCPServer(state.registry, state.pool, state.jwt)
    mcp_app = build_mcp_app(mcp_server)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Запускаем session manager MCP напрямую в нашем lifespan. Внутри
        # session_manager.run() SDK создаёт anyio task group; при shutdown
        # (после tg.cancel_scope.cancel() в finally) выход из него падает с
        # "RuntimeError: Attempted to exit a cancel scope ...". Это известная
        # несовместимость SDK (python-sdk #922) — сессия при этом корректно
        # закрывается, поэтому перехватываем именно этот RuntimeError.
        try:
            async with mcp_server.session_manager.run():
                await discover_tools(state)
                yield
                await state.pool.shutdown()
        except RuntimeError as exc:
            if "cancel scope" not in str(exc):
                raise

    app = FastAPI(
        title="mcp_router",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.app = state
    app.mount(MCP_MOUNT, mcp_app, name="mcp")
    app.include_router(api.router)
    app.include_router(run_api.router)
    app.include_router(admin.router)

    return app


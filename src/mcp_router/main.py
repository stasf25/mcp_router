"""Точка входа FastAPI-приложения mcp_router.

Загружает config.yaml, строит AppState (registry + pool), монтирует
единый MCP-endpoint на /mcp, подключает REST API (/api) и админку (/).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import api
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

    mcp_server = ScopedMCPServer(state.registry, state.pool)
    mcp_app = build_mcp_app(mcp_server)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mcp_lifespan = mcp_app.router.lifespan_context
        async with mcp_lifespan(mcp_app):
            await discover_tools(state)
            yield
            await state.pool.shutdown()

    app = FastAPI(
        title="mcp_router",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.app = state
    app.mount(MCP_MOUNT, mcp_app, name="mcp")
    app.include_router(api.router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def admin_index() -> str:
        return ADMIN_HTML

    return app


ADMIN_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>mcp_router — admin</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem;background:#111;color:#eee}
 h1{color:#fff}
 h2{border-bottom:1px solid #333;padding-bottom:.3rem}
 table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}
 th,td{border:1px solid #333;padding:.4rem .6rem;text-align:left}
 .ok{color:#6f6}.err{color:#f66}.off{color:#888}
 pre{background:#1a1a1a;padding:.5rem;overflow:auto}
</style>
</head>
<body>
<h1>mcp_router</h1>
<h2>Серверы</h2><table id="servers"><thead><tr><th>Имя</th><th>URL</th><th>Enabled</th><th>Статус</th></tr></thead><tbody></tbody></table>
<h2>Группы</h2><table id="groups"><thead><tr><th>Имя</th><th>Enabled</th><th>Tools</th></tr></thead><tbody></tbody></table>
<h2>Tools</h2><table id="tools"><thead><tr><th>Tool</th><th>Сервер</th><th>Доступен</th><th>Группы</th></tr></thead><tbody></tbody></table>
<script>
async function load(){
 const [s,g,t]=await Promise.all([
   fetch('/api/servers').then(r=>r.json()),
   fetch('/api/groups').then(r=>r.json()),
   fetch('/api/tools').then(r=>r.json()),
 ]);
 const sb=document.querySelector('#servers tbody');
 s.forEach(x=>{const tr=document.createElement('tr');
  tr.innerHTML=`<td>${x.name}</td><td>${x.url}</td><td>${x.enabled}</td><td class="${x.status}">${x.status}</td>`;
  sb.appendChild(tr);});
 const gb=document.querySelector('#groups tbody');
 g.forEach(x=>{const tr=document.createElement('tr');
  tr.innerHTML=`<td>${x.name}</td><td>${x.enabled}</td><td>${x.tools.join(', ')}</td>`;
  gb.appendChild(tr);});
 const tb=document.querySelector('#tools tbody');
 t.forEach(x=>{const tr=document.createElement('tr');
  tr.innerHTML=`<td>${x.name}</td><td>${x.server}</td><td class="${x.available?'ok':'err'}">${x.available}</td><td>${x.groups.join(', ')}</td>`;
  tb.appendChild(tr);});
}
load();
</script>
</body>
</html>"""

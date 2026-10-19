"""Контрактные тесты: mock MCP-сервер + роутер, проверка MCP-протокола.

Запускает mock нижестоящий MCP-сервер и сам роутер в фоновых uvicorn-потоках,
подключается внешним MCP-клиентом к /mcp, /mcp/{group}, /mcp/{server}.{tool}.
"""

from __future__ import annotations

import time

import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mock_mcp import free_port, serve_mcp_server_on_thread

from mcp_router.main import create_app


def wait_http_ready(port, path="/"):
    import httpx

    for _ in range(80):
        try:
            r = httpx.get(f"http://127.0.0.1:{port}{path}")
            if path == "/" or r.status_code != 404:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


@pytest.fixture
def router_url(running_mock_server):
    """Запускает роутер, подключённый к running_mock_server, возвращает base URL."""
    cfg = f"""
servers:
  - name: mock
    url: {running_mock_server}
    enabled: true
    timeout: 30
groups:
  - name: web
    tools: [mock.navigate, mock.click]
    enabled: true
  - name: screens
    tools: [mock.screenshot]
    enabled: true
"""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        cfg_path = f.name

    app = create_app(cfg_path)
    port = free_port()
    serve_mcp_server_on_thread(app, port)
    assert wait_http_ready(port, "/api/health")
    # даём lifespan завершить подключение к mock
    time.sleep(0.6)
    return f"http://127.0.0.1:{port}"


async def list_tool_names(url):
    async with Client(streamable_http_client(url)) as c:
        res = await c.list_tools()
        return [t.name for t in res.tools]


@pytest.mark.anyio
async def test_handshake_and_all_tools(router_url):
    names = await list_tool_names(f"{router_url}/mcp")
    assert names == ["navigate", "click", "screenshot"]


@pytest.mark.anyio
async def test_group_scope(router_url):
    names = await list_tool_names(f"{router_url}/mcp/web")
    assert names == ["navigate", "click"]


@pytest.mark.anyio
async def test_second_group_scope(router_url):
    names = await list_tool_names(f"{router_url}/mcp/screens")
    assert names == ["screenshot"]


@pytest.mark.anyio
async def test_single_tool_scope(router_url):
    names = await list_tool_names(f"{router_url}/mcp/mock.click")
    assert names == ["click"]


@pytest.mark.anyio
async def test_call_tool_via_group(router_url):
    async with Client(streamable_http_client(f"{router_url}/mcp/web")) as c:
        res = await c.call_tool("mock.navigate", {"url": "https://example.com"})
        text = "".join(x.text for x in res.content if getattr(x, "type", None) == "text")
        assert text == "navigated:https://example.com"


@pytest.mark.anyio
async def test_call_tool_out_of_scope_returns_error(router_url):
    async with Client(streamable_http_client(f"{router_url}/mcp/web")) as c:
        res = await c.call_tool("mock.screenshot", {"path": "x.png"})
        assert res.is_error is True

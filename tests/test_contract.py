"""Контрактные тесты: mock MCP-сервер + роутер, проверка MCP-протокола.

Запускает mock нижестоящий MCP-сервер и сам роутер в фоновых uvicorn-потоках,
подключается внешним MCP-клиентом к /mcp, /mcp/{group}, /mcp/{server}.{tool}.
"""

from __future__ import annotations

import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


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


def test_tool_detail_endpoint(router_url):
    import httpx

    r = httpx.get(f"{router_url}/api/tool/mock.navigate")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "mock.navigate"
    assert body["server"] == "mock"
    assert body["available"] is True
    assert body["groups"] == ["web"]
    assert body["input_schema"]["type"] == "object"
    assert "url" in body["input_schema"]["properties"]


def test_tool_detail_unknown_404(router_url):
    import httpx

    r = httpx.get(f"{router_url}/api/tool/mock.nope")
    assert r.status_code == 404


def test_admin_page_live(router_url):
    import httpx

    r = httpx.get(f"{router_url}/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "tool-modal" in r.text
    assert "tools-head" in r.text

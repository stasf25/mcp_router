"""Контрактные тесты: mock MCP-сервер + роутер, проверка MCP-протокола.

Запускает mock нижестоящий MCP-сервер и сам роутер в фоновых uvicorn-потоках,
подключается внешним MCP-клиентом к /mcp, /mcp/{group}, /mcp/{server}.{tool}.
"""

from __future__ import annotations

import json

import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


def _read_text(res) -> str:
    return "".join(x.text for x in res.content if getattr(x, "type", None) == "text")


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


@pytest.mark.anyio
async def test_session_id_passthrough_to_downstream(router_url_with_session):
    """Проброс session_id (v1-клиент → v2-сервер): нижестоящий получает в _meta ключ из конфига.

    Внешний v1-клиент (mode="legacy", initialize handshake) подключается к /mcp;
    роутер (с session_id_key: my.router.session) передаёт session_id внешней
    сессии в _meta исходящего tools/call. Capture-сервер возвращает полученный
    _meta, что подтверждает сквозной проброс.
    """
    async with Client(streamable_http_client(f"{router_url_with_session}/mcp"), mode="legacy") as c:
        res = await c.call_tool("capture.echo_meta", {})
        meta = json.loads(_read_text(res))
        assert res.is_error is False
        assert isinstance(meta.get("my.router.session"), str)
        assert len(meta["my.router.session"]) > 0


@pytest.mark.anyio
async def test_session_id_distinguishes_clients(router_url_with_session):
    """Две внешние v1-сессии дают разные session_id (не перепутаны между клиентами)."""
    async with Client(
        streamable_http_client(f"{router_url_with_session}/mcp"), mode="legacy"
    ) as ca:
        async with Client(
            streamable_http_client(f"{router_url_with_session}/mcp"), mode="legacy"
        ) as cb:
            ra = await ca.call_tool("capture.echo_meta", {})
            rb = await cb.call_tool("capture.echo_meta", {})
            meta_a = json.loads(_read_text(ra))
            meta_b = json.loads(_read_text(rb))
            assert meta_a["my.router.session"] != meta_b["my.router.session"]


@pytest.mark.anyio
async def test_session_id_stable_within_client(router_url_with_session):
    """Одна внешняя v1-сессия в течение жизни даёт один и тот же session_id."""
    async with Client(streamable_http_client(f"{router_url_with_session}/mcp"), mode="legacy") as c:
        r1 = await c.call_tool("capture.echo_meta", {})
        r2 = await c.call_tool("capture.echo_meta", {})
        m1 = json.loads(_read_text(r1))
        m2 = json.loads(_read_text(r2))
        assert m1["my.router.session"] == m2["my.router.session"]


@pytest.mark.anyio
async def test_session_id_passthrough_disabled_by_default(router_url_with_session_default_off):
    """Без session_id_key в конфиге проброс выключен: в _meta нет ключа (обратная совместимость)."""
    async with Client(
        streamable_http_client(f"{router_url_with_session_default_off}/mcp"), mode="legacy"
    ) as c:
        res = await c.call_tool("capture.echo_meta", {})
        meta = json.loads(_read_text(res))
        assert res.is_error is False
        assert "my.router.session" not in meta


@pytest.mark.anyio
async def test_v2_meta_passthrough_to_downstream(router_url_with_session_default_off):
    """v2→v2 pass-through: пользовательские ключи _meta доходят до нижестоящего v2-сервера.

    Внешний v2-клиент кладёт пользовательские ключи в _meta вызова; роутер
    пробрасывает их транзитом нижестоящему v2-серверу. Зарезервированные ключи
    io.modelcontextprotocol/* исключаются из проброса.
    """
    async with Client(streamable_http_client(f"{router_url_with_session_default_off}/mcp")) as c:
        res = await c.call_tool(
            "capture.echo_meta",
            {},
            meta={
                "tenant": "acme",
                "request.user": "alice",
                "io.modelcontextprotocol/forbidden": "must-be-filtered",
            },
        )
        meta = json.loads(_read_text(res))
        assert res.is_error is False
        assert meta.get("tenant") == "acme"
        assert meta.get("request.user") == "alice"
        # зарезервированные ключи из входящего _meta не пробрасываются
        assert "io.modelcontextprotocol/forbidden" not in meta

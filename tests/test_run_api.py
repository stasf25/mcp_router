"""Тесты этапа 3: JWT-скоуп (/run/select) и прямой вызов (/run/tool/call)."""

from __future__ import annotations

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_router.jwt_service import JWTError, JWTService

_SECRET = "test-secret-0123456789abcdef0123456789abcdef"


def test_jwt_issue_decode_roundtrip():
    svc = JWTService(secret=_SECRET)
    token = svc.issue(["a.b", "c.d"], ttl=60)
    assert svc.decode(token) == ["a.b", "c.d"]


def test_jwt_expired_raises():
    svc = JWTService(secret=_SECRET)
    token = svc.issue(["a.b"], ttl=-1)
    with pytest.raises(JWTError):
        svc.decode(token)


def test_jwt_wrong_secret_raises():
    token = JWTService(secret=_SECRET).issue(["a.b"], ttl=60)
    with pytest.raises(JWTError):
        JWTService(secret="x" * 32).decode(token)


def test_run_select_returns_openai_tools(router_url):
    r = httpx.post(
        f"{router_url}/run/select",
        json={"tools": ["mock.navigate", "mock.click"], "ttl": 3600},
    )
    assert r.status_code == 200
    body = r.json()
    assert "token" in body
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == ["mock.navigate", "mock.click"]
    assert body["tools"][0]["type"] == "function"
    # tools перечислены в OpenAI-формате
    assert "parameters" in body["tools"][0]["function"]


def test_run_tool_call_direct(router_url):
    r = httpx.post(
        f"{router_url}/run/tool/call",
        json={"name": "mock.navigate", "arguments": {"url": "https://example.com"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    texts = [c["text"] for c in body["result"]["content"] if c.get("type") == "text"]
    assert texts == ["navigated:https://example.com"]
    assert body["result"]["isError"] is False


def test_run_tool_call_unknown_404(router_url):
    r = httpx.post(
        f"{router_url}/run/tool/call",
        json={"name": "mock.nope", "arguments": {}},
    )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_mcp_jwt_scopes_tools(router_url):
    """MCP-endpoint /mcp с JWT в header отдаёт только tools из токена."""
    token = httpx.post(
        f"{router_url}/run/select",
        json={"tools": ["mock.click"]},
    ).json()["token"]
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}
    ) as http:
        async with Client(
            streamable_http_client(f"{router_url}/mcp", http_client=http)
        ) as c:
            res = await c.list_tools()
            assert [t.name for t in res.tools] == ["click"]


@pytest.mark.anyio
async def test_mcp_jwt_via_query_token(router_url):
    token = httpx.post(
        f"{router_url}/run/select",
        json={"tools": ["mock.screenshot"]},
    ).json()["token"]
    async with Client(
        streamable_http_client(f"{router_url}/mcp?token={token}")
    ) as c:
        res = await c.list_tools()
        assert [t.name for t in res.tools] == ["screenshot"]


@pytest.mark.anyio
async def test_mcp_jwt_invalid_returns_empty(router_url):
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer not.a.valid.jwt"}
    ) as http:
        async with Client(
            streamable_http_client(f"{router_url}/mcp", http_client=http)
        ) as c:
            res = await c.list_tools()
            assert res.tools == []

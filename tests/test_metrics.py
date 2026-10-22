"""Тесты Prometheus-метрик: вызовы tools, статусы серверов, HTTP, /metrics."""

from __future__ import annotations

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_router.metrics import normalize_path


def _has_series(text: str, name: str, **labels) -> bool:
    """Проверяет наличие серии метрики name с заданными label'ами."""
    for line in text.splitlines():
        if line.startswith(name + "{"):
            brace = line.index("{")
            sep = line.rfind("} ")
            pairs: dict[str, str] = {}
            for part in line[brace + 1 : sep].split(","):
                if "=" in part:
                    k, _, v = part.partition("=")
                    pairs[k.strip()] = v.strip().strip('"')
            if all(pairs.get(k) == v for k, v in labels.items()):
                return True
    return False


def test_normalize_path():
    assert normalize_path("/mcp") == "/mcp"
    assert normalize_path("/mcp/web") == "/mcp"
    assert normalize_path("/mcp/mock.navigate") == "/mcp"
    assert normalize_path("/run/select") == "/run/select"
    assert normalize_path("/run/select/web") == "/run/select/{group}"
    assert normalize_path("/run/tool/call") == "/run/tool/call"
    assert normalize_path("/api/tool/mock.navigate") == "/api/tool/{tool}"
    assert normalize_path("/metrics") == "/metrics"
    assert normalize_path("/api/servers/mock/list") == "/api/servers/mock/list"


def test_metrics_endpoint_returns_exposition(router_url):
    r = httpx.get(f"{router_url}/metrics")
    assert r.status_code == 200
    assert "mcp_router_tool_calls_total" in r.text
    assert "mcp_router_http_requests_total" in r.text


def test_run_tool_call_records_metric_ok(router_url):
    r = httpx.post(
        f"{router_url}/run/tool/call",
        json={"name": "mock.navigate", "arguments": {"url": "https://example.com"}},
    )
    assert r.status_code == 200
    m = httpx.get(f"{router_url}/metrics").text
    assert _has_series(
        m, "mcp_router_tool_calls_total", server="mock", tool="navigate", status="ok"
    )


@pytest.mark.anyio
async def test_mcp_tool_call_records_metric(router_url):
    """MCP tools/call на /mcp тоже пишет метрику вызова."""
    async with Client(streamable_http_client(f"{router_url}/mcp")) as c:
        res = await c.call_tool("mock.click", {"selector": "#x"})
        assert res.is_error is False
    m = httpx.get(f"{router_url}/metrics").text
    assert _has_series(m, "mcp_router_tool_calls_total", server="mock", tool="click", status="ok")


def test_server_status_gauges_present(router_url):
    m = httpx.get(f"{router_url}/metrics").text
    assert _has_series(m, "mcp_router_server_up", server="mock")
    assert _has_series(m, "mcp_router_server_status", server="mock")
    assert _has_series(m, "mcp_router_server_tools_total", server="mock")


def test_http_metric_normalized_path(router_url):
    httpx.post(f"{router_url}/run/select/web", json={})
    m = httpx.get(f"{router_url}/metrics").text
    assert _has_series(
        m, "mcp_router_http_requests_total", method="POST", path="/run/select/{group}", status="200"
    )

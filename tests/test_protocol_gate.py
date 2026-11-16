"""Тесты глобального env-ключа MCP_PROTOCOL_VERSION (§4.6 ТЗ).

Проверяют, что роутер в роли MCP-сервера на /mcp принимает/отклоняет
клиентов нужной версии протокола (по заголовку mcp-protocol-version),
и что невалидное значение переменной — ошибка конфигурации (fail fast).
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_router.config import ConfigError, protocol_version_mode


def _write_cfg(url: str) -> str:
    cfg = f"""servers:
  - name: mock
    url: {url}
    enabled: true
    timeout: 30
groups:
  - name: all
    tools: [mock.navigate]
    enabled: true
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        return f.name


def _start_router(mode: str, running_mock_server):
    from mcp_router.main import create_app
    from tests.conftest import _wait_http_ready
    from tests.mock_mcp import free_port, serve_mcp_server_on_thread

    cfg_path = _write_cfg(running_mock_server)
    os.environ["MCP_PROTOCOL_VERSION"] = mode
    try:
        app = create_app(cfg_path)
    finally:
        os.environ.pop("MCP_PROTOCOL_VERSION", None)
    port = free_port()
    serve_mcp_server_on_thread(app, port)
    assert _wait_http_ready(port, "/api/health")
    time.sleep(0.6)
    return f"http://127.0.0.1:{port}"


@pytest.mark.anyio
async def test_v2_client_ok_in_v2_mode(running_mock_server):
    url = _start_router("v2", running_mock_server)
    async with Client(streamable_http_client(f"{url}/mcp")) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        assert "navigate" in names


@pytest.mark.anyio
async def test_v1_client_rejected_in_v2_mode(running_mock_server):
    url = _start_router("v2", running_mock_server)
    with pytest.raises(Exception):
        async with Client(streamable_http_client(f"{url}/mcp"), mode="legacy") as c:
            await c.list_tools()


@pytest.mark.anyio
async def test_v1_client_ok_in_v1_mode(running_mock_server):
    url = _start_router("v1", running_mock_server)
    async with Client(streamable_http_client(f"{url}/mcp"), mode="legacy") as c:
        names = [t.name for t in (await c.list_tools()).tools]
        assert "navigate" in names


@pytest.mark.anyio
async def test_v2_client_rejected_in_v1_mode(running_mock_server):
    """v1-режим отклоняет строго-v2 клиента (mode='2026-07-28', без fallback на v1)."""
    url = _start_router("v1", running_mock_server)
    with pytest.raises(Exception):
        async with Client(streamable_http_client(f"{url}/mcp"), mode="2026-07-28") as c:
            await c.list_tools()


@pytest.mark.anyio
async def test_auto_mode_accepts_both_eras(running_mock_server):
    url = _start_router("auto", running_mock_server)
    async with Client(streamable_http_client(f"{url}/mcp")) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        assert "navigate" in names
    async with Client(streamable_http_client(f"{url}/mcp"), mode="legacy") as c:
        names = [t.name for t in (await c.list_tools()).tools]
        assert "navigate" in names


def test_protocol_version_mode_default_auto(monkeypatch):
    monkeypatch.delenv("MCP_PROTOCOL_VERSION", raising=False)
    assert protocol_version_mode() == "auto"


def test_protocol_version_mode_valid_values(monkeypatch):
    for value in ("auto", "v1", "v2"):
        monkeypatch.setenv("MCP_PROTOCOL_VERSION", value)
        assert protocol_version_mode() == value


@pytest.mark.parametrize("bad", ["bogus", "3.0", "2025-11-25", "legacy"])
def test_protocol_version_mode_invalid_raises(monkeypatch, bad):
    monkeypatch.setenv("MCP_PROTOCOL_VERSION", bad)
    with pytest.raises(ConfigError):
        protocol_version_mode()

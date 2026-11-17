"""Тесты логирования подключений: downstream-серверов и внешних клиентов.

Проверяют, что:
- при подключении к нижестоящему серверу пишется ``server.connected``
  с согласованной версией протокола и её эрой;
- при установлении соединения внешнего клиента на /mcp пишется
  ``mcp.client_connected`` с эрой (v1/v2), и это логирование не влияет
  на прохождение запроса (в т.ч. на гейт MCP_PROTOCOL_VERSION).
"""

from __future__ import annotations

import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from structlog.testing import capture_logs

from mcp_router.config import ServerConfig
from mcp_router.mcp_client import ClientPool
from mcp_router.registry import Registry


def _find(logs, event):
    return [e for e in logs if e.get("event") == event]


@pytest.mark.anyio
async def test_downstream_connected_logs_protocol_version(running_mock_server):
    server = ServerConfig(
        name="mock",
        url=running_mock_server,
        enabled=True,
        timeout=30,
    )
    registry = Registry(servers=[server], groups=[])
    pool = ClientPool(registry)
    with capture_logs() as logs:
        await pool.start()
        try:
            connected = _find(logs, "server.connected")
            assert connected, f"нет server.connected, логи: {logs}"
            entry = connected[0]
            assert entry["server"] == "mock"
            assert entry["protocol_era"] in ("v1", "v2")
            assert entry["protocol_version"]
        finally:
            await pool.shutdown()


@pytest.mark.anyio
async def test_client_connected_v1_legacy(running_mock_server):
    from mcp_router.main import create_app
    from tests.conftest import _wait_http_ready
    from tests.mock_mcp import free_port, serve_mcp_server_on_thread

    cfg = f"""servers:
  - name: mock
    url: {running_mock_server}
    enabled: true
    timeout: 30
groups:
  - name: all
    tools: [mock.navigate]
    enabled: true
"""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        cfg_path = f.name
    app = create_app(cfg_path)
    port = free_port()
    serve_mcp_server_on_thread(app, port)
    assert _wait_http_ready(port, "/api/health")

    with capture_logs() as logs:
        transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp")
        async with Client(transport, mode="legacy") as c:
            await c.list_tools()

    connected = _find(logs, "mcp.client_connected")
    assert connected, f"нет mcp.client_connected, логи: {logs}"
    assert connected[0]["version"] == "v1"


@pytest.mark.anyio
async def test_logging_does_not_break_protocol_gate(running_mock_server, monkeypatch):
    """Логирование подключений не влияет на гейт MCP_PROTOCOL_VERSION.

    При MCP_PROTOCOL_VERSION=v1:
    - v2-клиент получает 406 (гейт срабатывает) — подключение отклонено и
      не логируется (CaptureContext находится внутри гейта, т.е. логирование
      не вмешивается в решение фильтра);
    - v1-клиент (legacy) проходит — его подключение логируется version=v1.
    """
    import os
    import tempfile

    from mcp_router.main import create_app
    from tests.conftest import _wait_http_ready
    from tests.mock_mcp import free_port, serve_mcp_server_on_thread

    cfg = f"""servers:
  - name: mock
    url: {running_mock_server}
    enabled: true
    timeout: 30
groups:
  - name: all
    tools: [mock.navigate]
    enabled: true
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        cfg_path = f.name
    os.environ["MCP_PROTOCOL_VERSION"] = "v1"
    try:
        app = create_app(cfg_path)
    finally:
        os.environ.pop("MCP_PROTOCOL_VERSION", None)
    port = free_port()
    serve_mcp_server_on_thread(app, port)
    assert _wait_http_ready(port, "/api/health")

    url = f"http://127.0.0.1:{port}/mcp"

    # v2-клиент: гейт v1 отклоняет (406), подключение не логируется.
    with capture_logs() as logs:
        transport = streamable_http_client(url)
        with pytest.raises(Exception):
            async with Client(transport, mode="2026-07-28") as c:
                await c.list_tools()
    assert not _find(logs, "mcp.client_connected"), (
        "отклонённое гейтом подключение не должно логироваться"
    )

    # v1-клиент (legacy): проходит гейт, подключение логируется version=v1.
    with capture_logs() as logs:
        transport = streamable_http_client(url)
        async with Client(transport, mode="legacy") as c:
            await c.list_tools()
    connected = _find(logs, "mcp.client_connected")
    assert connected, f"нет mcp.client_connected, логи: {logs}"
    assert connected[0]["version"] == "v1"


@pytest.mark.anyio
async def test_client_connected_v2_modern(running_mock_server):
    from mcp_router.main import create_app
    from tests.conftest import _wait_http_ready
    from tests.mock_mcp import free_port, serve_mcp_server_on_thread

    cfg = f"""servers:
  - name: mock
    url: {running_mock_server}
    enabled: true
    timeout: 30
groups:
  - name: all
    tools: [mock.navigate]
    enabled: true
"""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        cfg_path = f.name
    app = create_app(cfg_path)
    port = free_port()
    serve_mcp_server_on_thread(app, port)
    assert _wait_http_ready(port, "/api/health")

    with capture_logs() as logs:
        async with Client(streamable_http_client(f"http://127.0.0.1:{port}/mcp")) as c:
            await c.list_tools()

    connected = _find(logs, "mcp.client_connected")
    assert connected, f"нет mcp.client_connected, логи: {logs}"
    assert connected[0]["version"] == "v2"

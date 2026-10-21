"""Общие фикстуры для тестов."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from mock_mcp import build_mock_server, free_port, serve_mcp_server_on_thread  # noqa: E402


@pytest.fixture
def mock_server_app():
    """Возвращает Starlette-app mock MCP-сервера."""
    return build_mock_server("mock").streamable_http_app(streamable_http_path="/mcp")


@pytest.fixture
def running_mock_server(mock_server_app):
    """Запускает mock MCP-сервер в фоновом потоке, возвращает его URL."""
    port = free_port()
    serve_mcp_server_on_thread(mock_server_app, port, "/mcp")
    return f"http://127.0.0.1:{port}/mcp"


def _wait_http_ready(port, path="/"):
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
    from mcp_router.main import create_app

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
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        cfg_path = f.name

    app = create_app(cfg_path)
    port = free_port()
    serve_mcp_server_on_thread(app, port)
    assert _wait_http_ready(port, "/api/health")
    time.sleep(0.6)
    return f"http://127.0.0.1:{port}"

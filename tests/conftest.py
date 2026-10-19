"""Общие фикстуры для тестов."""

from __future__ import annotations

import sys
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

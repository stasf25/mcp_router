"""Интеграционный тест graceful degradation при недоступности нижестоящего сервера.

Проверяет ключевое свойство из ТЗ §5.1: при падении нижестоящего сервера
mcp_router не падает целиком, а возвращает результат с ``isError: true``
(через /run/tool/call), а после срабатывания circuit breaker — продолжает
отвечать 200, не выбрасывая HTTP-ошибку.
"""

from __future__ import annotations

import tempfile
import time

import httpx
import pytest
from mock_mcp import FailSwitch, build_flaky_mock_server, free_port, serve_mcp_server_on_thread

from mcp_router.main import create_app


@pytest.fixture
def degraded_router():
    """Роутер с flaky-сервером (порог breaker = 2) для эмуляции недоступности."""
    switch = FailSwitch()
    server = build_flaky_mock_server("flaky", switch)
    mock_app = server.streamable_http_app(streamable_http_path="/mcp")
    mock_port = free_port()
    serve_mcp_server_on_thread(mock_app, mock_port, "/mcp")
    mock_url = f"http://127.0.0.1:{mock_port}/mcp"

    cfg = f"""
servers:
  - name: flaky
    url: {mock_url}
    enabled: true
    timeout: 30
    circuit_breaker:
      threshold: 2
      window: 60.0
      recovery: 30.0
groups:
  - name: web
    tools: [flaky.ping]
    enabled: true
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        cfg_path = f.name

    app = create_app(cfg_path)
    port = free_port()
    serve_mcp_server_on_thread(app, port)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health").status_code != 404:
                break
        except Exception:
            pass
        time.sleep(0.1)
    time.sleep(0.6)
    base = f"http://127.0.0.1:{port}"
    return base, switch


def _call(base: str) -> httpx.Response:
    return httpx.post(f"{base}/run/tool/call", json={"name": "flaky.ping", "arguments": {}})


def test_healthy_call_ok(degraded_router):
    base, switch = degraded_router
    r = _call(base)
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["isError"] is False
    texts = [c["text"] for c in body["result"]["content"] if c.get("type") == "text"]
    assert texts == ["pong"]


def test_graceful_degradation_when_server_fails(degraded_router):
    """Падение сервера: вызовы возвращают isError: true, а не HTTP-ошибку."""
    base, switch = degraded_router
    switch.fail = True
    # Пока breaker не открылся — единичные ошибки проксируются как isError: true.
    for _ in range(3):
        r = _call(base)
        assert r.status_code == 200, "роутер должен отвечать 200 даже при сбое сервера"
        body = r.json()
        assert body["result"]["isError"] is True


def test_circuit_breaker_open_still_returns_200(degraded_router):
    """После срабатывания breaker роутер не падает, а отдаёт 200 с isError: true."""
    base, switch = degraded_router
    switch.fail = True
    # Несколько вызовов, чтобы наверняка превысить порог (2) и открыть breaker.
    for _ in range(4):
        _call(base)
    # Breaker открыт — последующие вызовы должны быть 200 (не 500), isError: true.
    r = _call(base)
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True

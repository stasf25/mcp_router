"""Тесты REST API через FastAPI TestClient (read-only, этап 1)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mcp_router.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_API_KEY", "test-secret")
    p = tmp_path / "config.yaml"
    p.write_text(
        """
servers:
  - name: playwright
    url: http://playwright:3000/mcp
    enabled: true
    timeout: 30
groups:
  - name: web
    tools: [playwright.navigate]
    enabled: true
"""
    )
    app = create_app(p)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")


def test_servers_list(client):
    r = client.get("/api/servers")
    assert r.status_code == 200
    servers = r.json()
    assert len(servers) == 1
    assert servers[0]["name"] == "playwright"
    assert servers[0]["status"] in ("connected", "disconnected", "error")


def test_groups_list(client):
    r = client.get("/api/groups")
    assert r.status_code == 200
    groups = r.json()
    assert groups[0]["name"] == "web"
    assert groups[0]["enabled"] is True


def test_tools_list(client):
    r = client.get("/api/tools")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_server_tools_unknown_404(client):
    r = client.get("/api/servers/nope/list")
    assert r.status_code == 404


def test_group_unknown_404(client):
    r = client.get("/api/groups/nope/list")
    assert r.status_code == 404


def test_admin_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "mcp_router" in r.text
    assert "tool-modal" in r.text
    assert "tools-head" in r.text


def test_tool_unknown_404(client):
    r = client.get("/api/tool/mock.nope")
    assert r.status_code == 404


def test_group_tools_list(client):
    r = client.get("/api/groups/web/list")
    assert r.status_code == 200
    assert r.json()["name"] == "web"

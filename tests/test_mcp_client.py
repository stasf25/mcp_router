"""Тесты circuit breaker и его подключения из конфига."""

from __future__ import annotations

import pytest

from mcp_router.config import ServerConfig
from mcp_router.mcp_client import CircuitBreaker, DownstreamClient


class FakeClient:
    """Подставной нижестоящий Client: перехватывает аргументы call_tool."""

    def __init__(self, protocol_version="2026-07-28"):
        self.calls = []
        self.protocol_version = protocol_version

    async def call_tool(self, name, arguments=None, *, meta=None, **kwargs):
        self.calls.append({"name": name, "arguments": arguments, "meta": meta})
        return type("R", (), {"is_error": False, "content": []})()


def _dc_with_fake(server_kwargs=None, session_id_key=..., protocol_version="2026-07-28"):
    server = ServerConfig(
        name="s",
        url="http://s/mcp",
        **(server_kwargs or {}),
    )
    if session_id_key is not ...:
        server.session_id_key = session_id_key
    dc = DownstreamClient(server, headers={})
    fake = FakeClient(protocol_version=protocol_version)
    dc._client = fake
    return dc, fake



def test_breaker_closed_by_default():
    cb = CircuitBreaker()
    assert cb.allow_request() is True


def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(threshold=3, window=60.0, recovery=30.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is True
    cb.record_failure()  # достигаем порога
    assert cb.allow_request() is False


def test_breaker_resets_on_success():
    cb = CircuitBreaker(threshold=2, window=60.0, recovery=30.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is False
    cb.record_success()
    assert cb.allow_request() is True


def test_breaker_half_open_after_recovery():
    cb = CircuitBreaker(threshold=2, window=60.0, recovery=0.01)
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is False
    # recovery истёк -> half-open (allow один пробный запрос)
    import time

    time.sleep(0.02)
    assert cb.allow_request() is True


def test_breaker_uses_config_params():
    """DownstreamClient берёт параметры breaker из конфига сервера."""
    server = ServerConfig(
        name="s",
        url="http://s/mcp",
        circuit_breaker={"threshold": 7, "window": 90.0, "recovery": 20.0},
    )
    dc = DownstreamClient(server, headers={})
    assert dc.breaker.threshold == 7
    assert dc.breaker.window == 90.0
    assert dc.breaker.recovery == 20.0


def test_breaker_config_defaults():
    server = ServerConfig(name="s", url="http://s/mcp")
    dc = DownstreamClient(server, headers={})
    assert dc.breaker.threshold == 5
    assert dc.breaker.window == 60.0
    assert dc.breaker.recovery == 30.0


@pytest.mark.anyio
async def test_call_tool_meta_present_when_key_configured():
    """При заданном session_id_key и session_id формируется _meta с этим ключом."""
    dc, fake = _dc_with_fake(session_id_key="my.key")
    await dc.call_tool("navigate", {"url": "x"}, session_id="sess-1")
    assert fake.calls[0]["meta"] == {"my.key": "sess-1"}


@pytest.mark.anyio
async def test_call_tool_meta_uses_default_key_when_session_id_present():
    """Дефолтный ключ используется, если он задан на сервере."""
    dc, fake = _dc_with_fake(session_id_key="mcp_router.session_id")
    await dc.call_tool("navigate", {"url": "x"}, session_id="sess-1")
    assert fake.calls[0]["meta"] == {"mcp_router.session_id": "sess-1"}


@pytest.mark.anyio
async def test_call_tool_no_meta_when_session_id_none():
    """session_id=None → meta не передаётся (даже если ключ задан)."""
    dc, fake = _dc_with_fake(session_id_key="my.key")
    await dc.call_tool("navigate", {"url": "x"}, session_id=None)
    assert fake.calls[0]["meta"] is None


@pytest.mark.anyio
async def test_call_tool_no_meta_when_key_default_none():
    """session_id_key=None (дефолт) → meta не формируется при любом session_id."""
    dc, fake = _dc_with_fake(session_id_key=None)
    await dc.call_tool("navigate", {"url": "x"}, session_id="sess-1")
    assert fake.calls[0]["meta"] is None


@pytest.mark.anyio
async def test_call_tool_no_meta_when_key_empty():
    """session_id_key='' (пустой) → meta не формируется при любом session_id."""
    dc, fake = _dc_with_fake(session_id_key="")
    await dc.call_tool("navigate", {"url": "x"}, session_id="sess-1")
    assert fake.calls[0]["meta"] is None


@pytest.mark.anyio
async def test_call_tool_meta_with_empty_string_session_value():
    """Пустая строка как значение session_id всё равно формирует meta (не None)."""
    dc, fake = _dc_with_fake(session_id_key="my.key")
    await dc.call_tool("navigate", {"url": "x"}, session_id="")
    assert fake.calls[0]["meta"] == {"my.key": ""}


def test_downstream_is_v2_true():
    dc, _ = _dc_with_fake(protocol_version="2026-07-28")
    assert dc.is_v2 is True


def test_downstream_is_v2_false_for_handshake():
    dc, _ = _dc_with_fake(protocol_version="2025-11-25")
    assert dc.is_v2 is False


def test_downstream_is_v2_false_without_client():
    dc = DownstreamClient(ServerConfig(name="s", url="http://s/mcp"), headers={})
    assert dc.protocol_version is None
    assert dc.is_v2 is False


@pytest.mark.anyio
async def test_call_tool_passthrough_meta_user_when_v2_downstream():
    """v2→v2: meta_user пробрасывается в _meta, когда нижестоящий сервер v2."""
    dc, fake = _dc_with_fake(protocol_version="2026-07-28")
    await dc.call_tool("navigate", {"url": "x"}, meta_user={"tenant": "acme"})
    assert fake.calls[0]["meta"] == {"tenant": "acme"}


@pytest.mark.anyio
async def test_call_tool_no_passthrough_meta_user_when_v1_downstream():
    """v2→v1: проброс невозможен, meta_user игнорируется (вариант 2 матрицы)."""
    dc, fake = _dc_with_fake(protocol_version="2025-11-25")
    await dc.call_tool("navigate", {"url": "x"}, meta_user={"tenant": "acme"})
    assert fake.calls[0]["meta"] is None


@pytest.mark.anyio
async def test_call_tool_session_id_takes_priority_over_meta_user():
    """v1-клиент (session_id) важнее meta_user: кладём session_id под session_id_key."""
    dc, fake = _dc_with_fake(session_id_key="my.key", protocol_version="2026-07-28")
    await dc.call_tool("navigate", {"url": "x"}, session_id="sess-1", meta_user={"tenant": "acme"})
    assert fake.calls[0]["meta"] == {"my.key": "sess-1"}


@pytest.mark.anyio
async def test_call_tool_no_meta_when_no_session_and_no_meta_user():
    dc, fake = _dc_with_fake(protocol_version="2026-07-28")
    await dc.call_tool("navigate", {"url": "x"})
    assert fake.calls[0]["meta"] is None


class _FakeReqCtx:
    def __init__(self, meta=None, session_id=None, headers=None):
        self.meta = meta
        self.session_id = session_id
        self.request = type("Req", (), {"headers": headers or {}})()


class _FakeContext:
    def __init__(self, meta=None, session_id=None, headers=None):
        self.request_context = _FakeReqCtx(meta, session_id, headers)


def _extract(context):
    from mcp_router.mcp_server import ScopedMCPServer

    return ScopedMCPServer._extract_client_context(context)


def test_extract_v2_meta_user_filters_reserved():
    ctx = _FakeContext(
        meta={
            "tenant": "acme",
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "x"},
        }
    )
    session_id, meta_user = _extract(ctx)
    assert session_id is None
    assert meta_user == {"tenant": "acme"}


def test_extract_v1_session_id_from_meta_absent():
    ctx = _FakeContext(meta=None, session_id="sess-1")
    session_id, meta_user = _extract(ctx)
    assert session_id == "sess-1"
    assert meta_user is None


def test_extract_v1_session_id_from_header():
    ctx = _FakeContext(meta=None, headers={"mcp-session-id": "sess-hdr"})
    session_id, meta_user = _extract(ctx)
    assert session_id == "sess-hdr"
    assert meta_user is None


def test_extract_none_context():
    session_id, meta_user = _extract(None)
    assert session_id is None
    assert meta_user is None

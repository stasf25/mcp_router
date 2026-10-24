"""Тесты circuit breaker и его подключения из конфига."""

from __future__ import annotations

from mcp_router.config import ServerConfig
from mcp_router.mcp_client import CircuitBreaker, DownstreamClient


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

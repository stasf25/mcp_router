"""Глобальное состояние приложения (один на процесс)."""

from __future__ import annotations

from dataclasses import dataclass

from .config import RouterConfig
from .mcp_client import ClientPool
from .registry import Registry


@dataclass
class AppState:
    config: RouterConfig
    registry: Registry
    pool: ClientPool

    @classmethod
    def build(cls, config: RouterConfig) -> "AppState":
        registry = Registry(config.servers, config.groups)
        pool = ClientPool(registry)
        return cls(config=config, registry=registry, pool=pool)

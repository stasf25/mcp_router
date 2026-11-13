"""Глобальное состояние приложения (один на процесс)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import RouterConfig
from .jwt_service import JWTService
from .mcp_client import ClientPool
from .registry import Registry

API_KEY_ENV = "MCP_ROUTER_API_KEY"


@dataclass
class AppState:
    config: RouterConfig
    registry: Registry
    pool: ClientPool
    jwt: JWTService
    api_key: str | None = None

    @classmethod
    def build(cls, config: RouterConfig) -> "AppState":
        registry = Registry(config.servers, config.groups)
        pool = ClientPool(registry)
        return cls(
            config=config,
            registry=registry,
            pool=pool,
            jwt=JWTService(),
            api_key=os.environ.get(API_KEY_ENV) or None,
        )

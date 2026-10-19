"""Конфигурация mcp_router: загрузка/валидация/сохранение config.yaml.

В config.yaml секреты хранятся как ИМЕНА переменных окружения (строки).
При загрузке эти имена резолвятся в реальные значения из ``os.environ``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_PATH_ENV = "MCP_ROUTER_CONFIG"


class AuthHeaders(BaseModel):
    """Произвольные заголовки аутентификации. Значения — имена env-переменных."""

    model_config = ConfigDict(extra="allow")

    def resolve(self, env: dict[str, str] | None = None) -> dict[str, str]:
        """Резолвит имена env-переменных в реальные значения заголовков."""
        env = env if env is not None else os.environ
        resolved: dict[str, str] = {}
        for name, value in self.model_dump().items():
            if isinstance(value, str) and value.startswith("env:"):
                env_name = value[4:]
                if env_name not in env:
                    raise ConfigError(
                        f"Переменная окружения '{env_name}' не найдена (заголовок '{name}')"
                    )
                resolved[name] = env[env_name]
            elif isinstance(value, str):
                env_name = value
                if env_name not in env:
                    raise ConfigError(
                        f"Переменная окружения '{env_name}' не найдена (заголовок '{name}')"
                    )
                resolved[name] = env[env_name]
            else:
                resolved[name] = value
        return resolved


class Auth(BaseModel):
    headers: AuthHeaders | None = None


class ServerConfig(BaseModel):
    name: str
    url: str
    auth: Auth | None = None
    enabled: bool = True
    timeout: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _validate_url(self) -> "ServerConfig":
        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ConfigError(f"URL сервера '{self.name}' должен начинаться с http(s)://")
        return self


class GroupConfig(BaseModel):
    name: str
    tools: list[str] = Field(default_factory=list)
    enabled: bool = True


class RouterConfig(BaseModel):
    servers: list[ServerConfig]
    groups: list[GroupConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_servers(self) -> "RouterConfig":
        names = [s.name for s in self.servers]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ConfigError(f"Дублирующиеся имена серверов: {sorted(dupes)}")
        return self

    def server(self, name: str) -> ServerConfig | None:
        for s in self.servers:
            if s.name == name:
                return s
        return None

    def group(self, name: str) -> GroupConfig | None:
        for g in self.groups:
            if g.name == name:
                return g
        return None


class ConfigError(Exception):
    """Ошибка конфигурации (валидация, отсутствие env-переменной)."""


def load_config(
    path: str | Path | None = None, env: dict[str, str] | None = None
) -> RouterConfig:
    """Загружает и валидирует конфиг из YAML-файла.

    Резолвит имена env-переменных в значения заголовков аутентификации.
    """
    env = env if env is not None else os.environ
    if path is None:
        path = Path(env.get(CONFIG_PATH_ENV, "config.yaml"))
    path = Path(path)

    if not path.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = RouterConfig.model_validate(raw)
    # Валидация на этапе загрузки: имена env-переменных для заголовков auth
    # должны существовать в окружении. Значения в модель не сохраняются
    # (в config хранятся только имена).
    for server in cfg.servers:
        if server.auth and server.auth.headers:
            server.auth.headers.resolve(env=env)
    return cfg


def config_to_dict(cfg: RouterConfig) -> dict[str, Any]:
    """Сериализует конфиг в словарь для записи в YAML."""
    return cfg.model_dump(mode="json", exclude_none=True)


def atomic_save(path: str | Path, data: dict[str, Any]) -> None:
    """Атомарная запись словаря в YAML-файл через временный файл + os.replace."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)

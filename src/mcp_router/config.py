"""Конфигурация mcp_router: загрузка/валидация/сохранение config.yaml.

Секреты передаются через ``${ENV_VAR}``-подстановку: перед парсингом YAML
во всём тексте конфига ``${VAR}`` заменяются значениями из окружения.
Это покрывает и ``url`` (например токен в query), и значения ``headers``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_PATH_ENV = "MCP_ROUTER_CONFIG"

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(text: str, env: dict[str, str]) -> str:
    """Заменяет все ``${VAR}`` во ``text`` значениями из ``env``.

    Бросает ``ConfigError``, если какая-либо переменная не найдена.
    """
    missing: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            missing.add(name)
            return match.group(0)
        return env[name]

    result = _ENV_VAR_RE.sub(_sub, text)
    if missing:
        raise ConfigError(
            "Переменные окружения не найдены: " + ", ".join(sorted(missing))
        )
    return result


class ServerConfig(BaseModel):
    name: str
    url: str
    headers: list[dict[str, str]] = Field(default_factory=list)
    enabled: bool = True
    timeout: float = Field(default=30.0, gt=0)

    @property
    def header_map(self) -> dict[str, str]:
        """Заголовки как плоский словарь (последний дубликат побеждает)."""
        merged: dict[str, str] = {}
        for h in self.headers:
            for k, v in h.items():
                merged[k] = v
        return merged

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

    Во всём тексте конфига выполняется ``${ENV_VAR}``-подстановка из окружения
    (fail fast на отсутствующих переменных), затем файл парсится как YAML.
    """
    env = env if env is not None else os.environ
    if path is None:
        path = Path(env.get(CONFIG_PATH_ENV, "config.yaml"))
    path = Path(path)

    if not path.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {path}")

    text = expand_env(path.read_text(encoding="utf-8"), env)
    raw = yaml.safe_load(text) or {}
    return RouterConfig.model_validate(raw)


def config_to_dict(cfg: RouterConfig) -> dict[str, Any]:
    """Сериализует конфиг в словарь для записи в YAML."""
    return cfg.model_dump(mode="json", exclude_none=True)


def atomic_save(path: str | Path, data: dict[str, Any]) -> None:
    """Атомарная запись словаря в YAML-файл через временный файл + os.replace."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)

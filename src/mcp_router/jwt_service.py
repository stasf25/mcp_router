"""JWT-сервис: выдача и проверка HMAC-подписанных токенов (этап 3).

Токен stateless: TTL встроен в claim ``exp``, подпись HMAC-SHA256. Секрет
берётся из env ``MCP_ROUTER_JWT_SECRET``; если не задан — сервис создаёт
случайный при старте (валидно для одного инстанса, но не для масштабирования).
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import jwt

JWT_SECRET_ENV = "MCP_ROUTER_JWT_SECRET"
_JWT_ALG = "HS256"


class JWTError(Exception):
    """Ошибка JWT: невалидная подпись, просрочен, отсутствует payload."""


class JWTService:
    """Выпускает и проверяет JWT с набором tools в payload."""

    def __init__(self, secret: str | None = None) -> None:
        self._secret = secret or os.environ.get(JWT_SECRET_ENV) or secrets.token_urlsafe(32)

    def issue(self, tools: list[str], ttl: int) -> str:
        payload: dict[str, Any] = {"tools": tools, "exp": int(time.time()) + max(ttl, 0)}
        return jwt.encode(payload, self._secret, algorithm=_JWT_ALG)

    def decode(self, token: str) -> list[str]:
        """Проверяет подпись и exp, возвращает список tools из payload."""
        try:
            payload = jwt.decode(token, self._secret, algorithms=[_JWT_ALG])
        except jwt.ExpiredSignatureError as exc:
            raise JWTError("JWT истёк") from exc
        except jwt.InvalidTokenError as exc:
            raise JWTError("Невалидный JWT") from exc
        tools = payload.get("tools")
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise JWTError("JWT не содержит корректный список tools")
        return tools

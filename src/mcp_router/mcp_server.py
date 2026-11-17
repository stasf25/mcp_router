"""Единый MCP-сервер наружу: ScopedMCPServer на `/mcp`.

Подкласс ``MCPServer`` (mcp==2.3.0). ``FilterSpec`` вычисляется на каждый запрос
из пути (агрегатор / группа / один tool), что автоматически отражает текущее
состояние реестра (availability серверов, группы, JWT).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult
from mcp_types import Tool as MCPTool
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from starlette.applications import Starlette

from .config import protocol_version_mode
from .jwt_service import ALL_TOOLS, JWTError, JWTService
from .mcp_client import ClientPool
from .registry import FilterSpec, Registry

MCP_MOUNT = "/mcp"

RESOLVED_ERROR = "Tool {name} not in scope"

logger = structlog.get_logger()


@dataclass
class RequestCtx:
    path: str
    headers: dict[str, str]
    query: dict[str, str] = None  # type: ignore[assignment]


_REQUEST_CTX: ContextVar[RequestCtx | None] = ContextVar("mcp_request_ctx", default=None)


def current_request() -> RequestCtx | None:
    return _REQUEST_CTX.get()


def _extract_jwt(ctx: RequestCtx | None) -> str | None:
    """Извлекает JWT из header Authorization: Bearer или query ?token=."""
    if ctx is None:
        return None
    auth = ctx.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if ctx.query:
        return ctx.query.get("token")
    return None


def _spec_from_path(path: str) -> FilterSpec:
    if path.startswith(MCP_MOUNT):
        rest = path[len(MCP_MOUNT):].strip("/")
    else:
        rest = path.strip("/")
    if not rest:
        return FilterSpec.all()
    if "." in rest:
        return FilterSpec.single(rest)
    return FilterSpec.for_group(rest)


class ScopedMCPServer(MCPServer):
    """MCP-сервер, отдающий tools согласно FilterSpec из пути запроса и JWT."""

    def __init__(
        self,
        registry: Registry,
        pool: ClientPool,
        jwt: JWTService,
        name: str = "mcp_router",
    ) -> None:
        super().__init__(name=name)
        self._registry = registry
        self._pool = pool
        self._jwt = jwt

    def _resolve_spec(self, context=None) -> FilterSpec:
        if context is not None:
            url = context.request_context.request.url
            path = url.path
            query = {}
            if url.query:
                parsed = parse_qs(url.query)
                query = {k: v[0] for k, v in parsed.items()}
            # Заголовки исходного запроса: у anyio-Request есть .headers (mapping),
            # ключи в lowercase; для некоторых запросов — через .scope.
            headers: dict[str, str] = {}
            request = context.request_context.request
            raw_headers = getattr(request, "headers", None)
            if raw_headers is not None:
                try:
                    headers = {k.lower(): v for k, v in raw_headers.items()}
                except Exception:  # noqa: BLE001
                    headers = {}
            elif hasattr(request, "scope"):
                headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in request.scope.get("headers", [])
                }
            ctx = RequestCtx(path=path, headers=headers, query=query)
        else:
            ctx = _REQUEST_CTX.get()
            path = ctx.path if ctx else ""
        jwt_token = _extract_jwt(ctx)
        if jwt_token:
            try:
                tools = self._jwt.decode(jwt_token)
            except JWTError:
                # Невалидный/просроченный JWT — по умолчанию безопасно не отдаём
                # ничего (принудительно пустой скоуп), вместо режима агрегатора.
                return FilterSpec(mode="jwt", tools=frozenset())
            if ALL_TOOLS in tools:
                # Токен «все tools» — режим агрегатора.
                return FilterSpec.all()
            return FilterSpec.jwt(tools)
        return _spec_from_path(path)

    async def list_tools(self) -> list[MCPTool]:
        spec = self._resolve_spec()
        return self._registry.mcp_tools(spec)

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None) -> CallToolResult:
        spec = self._resolve_spec(context)
        if not self._registry.is_tool_available(name, spec):
            raise ToolNotAvailableError(RESOLVED_ERROR.format(name=name))
        session_id, meta_user = self._extract_client_context(context)
        return await self._pool.call_tool(
            name, arguments, session_id=session_id, meta_user=meta_user
        )

    _RESERVED_META_PREFIX = "io.modelcontextprotocol/"

    @classmethod
    def _extract_client_context(cls, context=None) -> tuple[str | None, dict[str, Any] | None]:
        """Определяет версию внешнего клиента и извлекает контекст для проброса.

        Возвращает ``(session_id, meta_user)``:
        - v2-клиент (входящий ``_meta`` несёт зарезервированные ключи
          ``io.modelcontextprotocol/*``): ``session_id=None``, ``meta_user`` —
          пользовательские ключи входящего ``_meta`` (исключая зарезервированные);
        - v1-клиент (иначе): ``session_id`` из контекста/заголовка
          ``mcp-session-id``, ``meta_user=None``.
        """
        incoming_meta = None
        if context is not None:
            try:
                incoming_meta = getattr(context.request_context, "meta", None)
            except Exception:  # noqa: BLE001
                incoming_meta = None
        if incoming_meta:
            user_keys = {
                k: v
                for k, v in incoming_meta.items()
                if not k.startswith(cls._RESERVED_META_PREFIX)
            }
            if user_keys:
                # v2-клиент: зарезервированные ключи присутствуют → это modern-путь.
                return None, user_keys
        session_id = cls._extract_session_id(context)
        return session_id, None

    @staticmethod
    def _extract_session_id(context=None) -> str | None:
        """Извлекает идентификатор внешней сессии из контекста запроса.

        Предпочитает ``context.request_context.session_id`` (когда SDK его
        предоставляет), иначе — HTTP-заголовок ``mcp-session-id`` (legacy
        Streamable HTTP / SSE). Возвращает ``None``, если сессия отсутствует
        (stdio, stateless) — тогда проброс не выполняется.
        """
        if context is None:
            return None
        try:
            sid = getattr(context.request_context, "session_id", None)
        except Exception:  # noqa: BLE001
            sid = None
        if sid:
            return sid
        request = getattr(context.request_context, "request", None)
        if request is not None:
            try:
                return request.headers.get("mcp-session-id")
            except Exception:  # noqa: BLE001
                return None
        return None


class ToolNotAvailableError(Exception):
    """Tool не входит в скоуп данного endpoint."""


class ProtocolVersionGateMiddleware:
    """Ограничивает принимаемую версию MCP-протокола на /mcp по заголовку.

    По спецификации MCP v2 (2026-07-28) HTTP-транспорт обязан передавать
    заголовок ``mcp-protocol-version``. Отсутствие заголовка —
    детерминированный признак v1-клиента; наличие заголовка со значением,
    не входящим в известный набор версий v1 (``HANDSHAKE_PROTOCOL_VERSIONS``
    из SDK), трактуется как v2. Такая логика учитывает и будущие версии
    протокола (до выхода v3). Тело не парсим: гейт принимает решение чисто
    по заголовку, в полном согласии с эрой-маршрутизацией самого SDK
    (streamable_http_manager).

    ``mode``: 'auto' (по умолчанию — принимать обе эры), 'v1' (только v1),
    'v2' (только v2). Отклонённый запрос получает HTTP 406.
    """

    _HDR = b"mcp-protocol-version"

    def __init__(self, app, mode: str = "auto") -> None:
        self.app = app
        self.mode = mode

    @staticmethod
    def _client_era(headers: dict[bytes, bytes] | None) -> str:
        """Определяет эру внешнего клиента по заголовку (v2 / v1).

        v1 — заголовок отсутствует или значение входит в известный набор
        версий v1 (``HANDSHAKE_PROTOCOL_VERSIONS`` из SDK). Всё остальное
        (заголовок присутствует со значением вне набора, напр. будущие
        2027-*) — v2.
        """
        if headers:
            for k, v in headers:
                if k.lower() == ProtocolVersionGateMiddleware._HDR:
                    value = v.decode("latin-1").strip()
                    if value not in HANDSHAKE_PROTOCOL_VERSIONS:
                        return "v2"
                    break
        return "v1"

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.mode != "auto":
            era = self._client_era(scope.get("headers"))
            denied = (self.mode == "v1" and era == "v2") or (
                self.mode == "v2" and era == "v1"
            )
            if denied:
                body = (
                    f'{{"error": "protocol version not allowed: '
                    f"mode={self.mode}, client={era}\"}}"
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 406,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class CaptureContextMiddleware:
    """Чистый ASGI-middleware: фиксирует path/headers в contextvar, ответ не буферизует.

    BaseHTTPMiddleware буферизует streaming-ответы (ломает SSE MCP), поэтому
    используем низкоуровневый ASGI-пропуск. Попутно логирует установление
    соединений внешних клиентов на /mcp (см. ``_log_connect``).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            query = {}
            qs = scope.get("query_string", b"")
            if qs:
                for pair in qs.decode("latin-1").split("&"):
                    if "=" in pair:
                        k, _, v = pair.partition("=")
                        query[k] = v
            token = _REQUEST_CTX.set(
                RequestCtx(path=scope["path"], headers=headers, query=query)
            )
            try:
                self._log_connect(scope["path"], headers)
                await self.app(scope, receive, send)
            finally:
                _REQUEST_CTX.reset(token)
        else:
            await self.app(scope, receive, send)

    @staticmethod
    def _log_connect(path: str, headers: dict[str, str]) -> None:
        """Логирует установление соединения внешнего клиента на /mcp.

        Единый механизм для v1 и v2 (без разбора тела):
        - v1-клиент: первый запрос сессии идёт без ``mcp-session-id`` и без
          modern-заголовка ``mcp-method``;
        - v2-клиент (stateless): ``server/discover`` приходит в modern-envelope
          с ``mcp-method: server/discover`` (и ``mcp-protocol-version``).

        Последующие v2-запросы (tools/list, tools/call и т.п.) несут иной
        ``mcp-method`` и потому подключением не считаются.

        Логирование чисто наблюдательное: читает только заголовки и не влияет
        на прохождение запроса, в т.ч. на гейт ``MCP_PROTOCOL_VERSION``.
        Любая ошибка логирования гасится, чтобы не сломать запрос.
        """
        try:
            if not path.startswith(MCP_MOUNT):
                return
            if headers.get("mcp-session-id"):
                return
            mcp_method = headers.get("mcp-method")
            if mcp_method is not None and mcp_method != "server/discover":
                return
            raw_headers = [
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in headers.items()
            ]
            era = ProtocolVersionGateMiddleware._client_era(raw_headers)
            logger.info(
                "mcp.client_connected",
                version=era,
                method=mcp_method or "initialize",
                client=headers.get("host"),
            )
        except Exception:  # noqa: BLE001 — лог не должен влиять на запрос
            pass


def build_mcp_app(
    server: ScopedMCPServer, protocol_mode: str | None = None
) -> Starlette:
    """Возвращает streamable_http_app сервера с ASGI-middleware захвата контекста.

    Используется wildcard-путь ``/{rest:path}``, чтобы один app обслуживал
    все подпути /mcp, /mcp/{group}, /mcp/{server}.{tool}. Middleware добавлен
    напрямую в inner-app (без обёртки), чтобы не потерять его lifespan,
    который инициализирует session manager (иначе 500 "Task group not initialized").

    DNS-rebinding protection включается SDK автоматически (т.к. host по умолчанию
    "127.0.0.1") и по умолчанию разрешает только localhost-хосты с портом. Внутри
    docker-сети к сервису ходят по имени ``mcp_router`` и часто с голым Host
    (без порта), поэтому расширяем список разрешённых хостов: точные имена +
    wildcard-паттерны с портом (SDK проверяет `:*` только если в Host есть порт).

    ``protocol_mode`` — значение ``MCP_PROTOCOL_VERSION`` (auto/v1/v2); если
    ``None`` — читается из env (fail fast на невалидном значении).
    """
    if protocol_mode is None:
        protocol_mode = protocol_version_mode()
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost",
            "localhost:*",
            "mcp_router",
            "mcp_router:*",
            "127.0.0.1",
            "127.0.0.1:*",
        ],
        allowed_origins=[],
    )
    inner = server.streamable_http_app(
        streamable_http_path="/{rest:path}",
        transport_security=transport_security,
    )
    if protocol_mode != "auto":
        inner.add_middleware(ProtocolVersionGateMiddleware, mode=protocol_mode)
    inner.add_middleware(CaptureContextMiddleware)
    return inner

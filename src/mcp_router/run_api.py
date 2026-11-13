"""Runtime REST API (этап 3): выдача JWT-скоупа и прямой вызов инструментов.

- ``POST /run/select`` — JWT на подмножество tools (или все, если список пуст).
- ``POST /run/select/{group}`` — JWT на подмножество tools из заданной группы
  (или вся группа, если список пуст).
- ``POST /run/tool/call`` — прямой вызов инструмента без MCP-клиента (ответ строкой).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .registry import Registry
from .state import AppState

router = APIRouter(prefix="/run", tags=["run"])


def _state(request: Request) -> AppState:
    return request.app.state.app


def _require_api_key(request: Request) -> None:
    """Проверяет Authorization: Bearer <key> против MCP_ROUTER_API_KEY.

    Если ключ не задан в окружении — доступ открыт (обратная совместимость).
    """
    state: AppState = request.app.state.app
    if not state.api_key:
        return
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Требуется API-ключ (Authorization: Bearer <key>)",
        )
    if auth[7:].strip() != state.api_key:
        raise HTTPException(status_code=401, detail="Неверный API-ключ")


router = APIRouter(
    prefix="/run",
    tags=["run"],
    dependencies=[Depends(_require_api_key)],
)


class SelectRequest(BaseModel):
    tools: list[str] | None = None
    ttl: int = Field(default=3600, ge=0)


class CallRequest(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)


def _to_openai(registry: Registry, qualified: str) -> dict:
    return registry.get_tool(qualified).to_openai()


def _select_response(state: AppState, tools: list[str], ttl: int) -> dict:
    token = state.jwt.issue(tools, ttl)
    payload = [_to_openai(state.registry, q) for q in tools]
    expires = state.jwt.expires_at(token)
    return {"token": token, "expires_at": expires, "tools": payload}


def _check_available(state: AppState, tools: list[str]) -> list[str]:
    """Возвращает tools, сервер которых недоступен (или tool не найден в реестре)."""
    out = []
    for q in tools:
        info = state.registry.get_tool(q)
        if info is None or not state.registry.server_available(info.server):
            out.append(q)
    return out


@router.post("/select")
async def select_tools(req: SelectRequest, state: AppState = Depends(_state)) -> dict:
    """Выпускает JWT на tools. Пустой/отсутствующий список — режим агрегатора (все tools)."""
    if not req.tools:
        token = state.jwt.issue(None, req.ttl)
        payload = [_to_openai(state.registry, t.qualified) for t in state.registry.all_tools()]
        return {"token": token, "expires_at": state.jwt.expires_at(token), "tools": payload}
    return await _select_subset(state, req.tools, req.ttl, state.registry)


async def _select_subset(
    state: AppState,
    tools: list[str],
    ttl: int,
    registry: Registry,
    valid: set[str] | None = None,
) -> dict:
    """Общая логика для подмножества tools: валидация + проверка доступности."""
    if valid is not None:
        missing = [q for q in tools if q not in valid]
    else:
        missing = [q for q in tools if registry.get_tool(q) is None]
    if missing:
        raise HTTPException(status_code=400, detail=f"Tools не найдены: {missing}")
    unavailable = _check_available(state, tools)
    if unavailable:
        raise HTTPException(status_code=400, detail=f"Tools недоступны: {unavailable}")
    return _select_response(state, tools, ttl)


@router.post("/select/{group}")
async def select_group_tools(
    group: str, req: SelectRequest, state: AppState = Depends(_state)
) -> dict:
    """Выпускает JWT на tools из заданной группы. Пустой список — вся группа."""
    registry: Registry = state.registry
    group_cfg = registry.groups.get(group)
    if group_cfg is None:
        raise HTTPException(status_code=404, detail=f"Группа '{group}' не найдена")
    valid = set(group_cfg.tools)
    if not req.tools:
        tools = [q for q in group_cfg.tools if registry.get_tool(q) is not None]
        unavailable = _check_available(state, tools)
        if unavailable:
            raise HTTPException(status_code=400, detail=f"Tools недоступны: {unavailable}")
        return _select_response(state, tools, req.ttl)
    return await _select_subset(state, req.tools, req.ttl, registry, valid=valid)


@router.post("/tool/call")
async def tool_call(req: CallRequest, state: AppState = Depends(_state)) -> dict:
    """Прямой вызов инструмента; результат в формате, эквивалентном MCP tools/call.

    Всегда отвечает 200. Некорректное имя или ошибка выполнения передаются
    внутри результата с ``isError: true`` и человекочитаемым ``content`` —
    чтобы LLM-loop не падал на HTTP-ошибке, а модель поняла, что tool недоступен.
    """
    info = state.registry.get_tool(req.name)
    if info is None:
        return _error_result(f"Tool '{req.name}' не найден")
    try:
        result = await state.pool.call_tool(req.name, req.arguments)
    except Exception as exc:  # noqa: BLE001
        return _error_result(f"Tool '{req.name}' вернул ошибку: {exc}")
    payload = {"content": result.content, "isError": result.is_error}
    dumped = json.loads(json.dumps(payload, default=_dump_content))
    return {"status": "success", "result": dumped}


def _error_result(message: str) -> dict:
    """Ответ об ошибке в формате, эквивалентном MCP tools/call (isError: true)."""
    return {
        "status": "success",
        "result": {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        },
    }


def _dump_content(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return str(obj)

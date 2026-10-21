"""Runtime REST API (этап 3): выдача JWT-скоупа и прямой вызов инструментов.

- ``POST /run/select``  — возвращает JWT + tools в OpenAI-формате по списку имён.
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


class SelectRequest(BaseModel):
    tools: list[str]
    ttl: int = Field(default=3600, ge=0)


class CallRequest(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)


@router.post("/select")
async def select_tools(req: SelectRequest, state: AppState = Depends(_state)) -> dict:
    """Выпускает JWT на подмножество tools и возвращает их в OpenAI-формате."""
    token = state.jwt.issue(req.tools, req.ttl)
    registry: Registry = state.registry
    tools = []
    for qualified in req.tools:
        info = registry.get_tool(qualified)
        if info is not None:
            tools.append(info.to_openai())
    return {"token": token, "expires_at": None, "tools": tools}


@router.post("/tool/call")
async def tool_call(req: CallRequest, state: AppState = Depends(_state)) -> dict:
    """Прямой вызов инструмента; результат в формате, эквивалентном MCP tools/call."""
    if state.registry.get_tool(req.name) is None:
        raise HTTPException(status_code=404, detail=f"Tool '{req.name}' не найден")
    try:
        result = await state.pool.call_tool(req.name, req.arguments)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    payload = {"content": result.content, "isError": result.is_error}
    dumped = json.loads(json.dumps(payload, default=_dump_content))
    return {"status": "success", "result": dumped}


def _dump_content(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return str(obj)

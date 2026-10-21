"""REST API (read-only, этап 1) + health check."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .registry import Registry, ServerStatus, ToolInfo
from .state import AppState

router = APIRouter(prefix="/api", tags=["api"])


def _state(request: Request) -> AppState:
    return request.app.state.app


def _tool_payload(registry: Registry, info: ToolInfo) -> dict:
    return {
        "name": info.qualified,
        "server": info.server,
        "description": info.schema.description,
        "input_schema": info.schema.input_schema,
        "available": registry.server_available(info.server),
        "groups": registry.tool_groups(info.qualified),
    }


@router.get("/health")
async def health(state: AppState = Depends(_state)) -> dict:
    connected = sum(
        1
        for s in state.registry.enabled_servers()
        if state.registry.status(s.name) == ServerStatus.CONNECTED
    )
    total = len(state.registry.enabled_servers())
    ready = total == 0 or connected == total
    return {
        "status": "ok" if ready else "degraded",
        "servers": {"connected": connected, "total": total},
    }


@router.get("/servers")
async def list_servers(state: AppState = Depends(_state)) -> list[dict]:
    out = []
    for server in state.registry.servers.values():
        out.append(
            {
                "name": server.name,
                "url": server.url,
                "enabled": server.enabled,
                "timeout": server.timeout,
                "status": state.registry.status(server.name).value,
            }
        )
    return out


@router.get("/servers/{name}/list")
async def server_tools(name: str, state: AppState = Depends(_state)) -> dict:
    if name not in state.registry.servers:
        raise HTTPException(status_code=404, detail=f"Сервер '{name}' не найден")
    tools = state.registry.server_tools(name)
    return {
        "server": name,
        "status": state.registry.status(name).value,
        "tools": [_tool_payload(state.registry, t) for t in tools],
    }


@router.get("/groups")
async def list_groups(state: AppState = Depends(_state)) -> list[dict]:
    out = []
    for group in state.registry.groups.values():
        tools = state.registry.group_tools(group.name)
        out.append(
            {
                "name": group.name,
                "enabled": group.enabled,
                "tools_count": len(tools),
                "tools": [t.qualified for t in tools],
            }
        )
    return out


@router.get("/groups/{name}/list")
async def group_tools(name: str, state: AppState = Depends(_state)) -> dict:
    if name not in state.registry.groups:
        raise HTTPException(status_code=404, detail=f"Группа '{name}' не найдена")
    group = state.registry.groups[name]
    tools = state.registry.group_tools(name)
    return {
        "name": name,
        "enabled": group.enabled,
        "tools": [_tool_payload(state.registry, t) for t in tools],
    }


@router.get("/tools")
async def list_tools(state: AppState = Depends(_state)) -> list[dict]:
    return [_tool_payload(state.registry, t) for t in state.registry.all_tools()]


@router.get("/tool/{qualified}")
async def get_tool(qualified: str, state: AppState = Depends(_state)) -> dict:
    """Полная информация по tool'у, включая json-схему вызова (input_schema)."""
    info = state.registry.get_tool(qualified)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Tool '{qualified}' не найден")
    return _tool_payload(state.registry, info)

"""Реестр tools и групп — центральная логика mcp_router.

Хранит маппинг `server.tool -> ToolInfo` и `group -> tools`, а также
предоставляет фильтрацию через `FilterSpec` (агрегатор / группа / один tool /
JWT-скоуп). Availability серверов обновляется фоновым health-check'ом.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from mcp_types import Tool as MCPTool

from .config import GroupConfig, ServerConfig


class ServerStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass
class ToolInfo:
    """Описание tool'а с нижестоящего сервера."""

    server: str
    name: str
    schema: MCPTool

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"

    def to_openai(self) -> dict:
        """Возвращает tool в формате OpenAI function calling."""
        input_schema = self.schema.input_schema
        return {
            "type": "function",
            "function": {
                "name": self.qualified,
                "description": self.schema.description or "",
                "parameters": input_schema,
            },
        }

    def to_mcp(self) -> MCPTool:
        return self.schema


@dataclass(frozen=True)
class FilterSpec:
    """Описывает, какие tools должен отдавать MCP-endpoint.

    mode:
      - "all"     — все tools со всех enabled серверов
      - "group"   — tools конкретной группы (group)
      - "single"  — один tool по qualified-имени (tool)
      - "jwt"     — подмножество tools из JWT-токена (tools)
    """

    mode: str
    group: str | None = None
    tool: str | None = None
    tools: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def all(cls) -> "FilterSpec":
        return cls(mode="all")

    @classmethod
    def for_group(cls, name: str) -> "FilterSpec":
        return cls(mode="group", group=name)

    @classmethod
    def single(cls, qualified: str) -> "FilterSpec":
        return cls(mode="single", tool=qualified)

    @classmethod
    def jwt(cls, tools: list[str]) -> "FilterSpec":
        return cls(mode="jwt", tools=frozenset(tools))


class Registry:
    """Реестр tools, групп и серверов."""

    def __init__(self, servers: list[ServerConfig], groups: list[GroupConfig]) -> None:
        self._servers: dict[str, ServerConfig] = {s.name: s for s in servers}
        self._groups: dict[str, GroupConfig] = {g.name: g for g in groups}
        self._tools: dict[str, ToolInfo] = {}          # qualified -> ToolInfo
        self._server_tools: dict[str, list[ToolInfo]] = {}
        self._status: dict[str, ServerStatus] = {}
        for name in self._servers:
            self._status[name] = ServerStatus.DISCONNECTED

    # --- серверы ---

    @property
    def servers(self) -> dict[str, ServerConfig]:
        return self._servers

    def enabled_servers(self) -> list[ServerConfig]:
        return [s for s in self._servers.values() if s.enabled]

    def status(self, name: str) -> ServerStatus:
        return self._status.get(name, ServerStatus.DISCONNECTED)

    def set_status(self, name: str, status: ServerStatus) -> None:
        if name in self._status:
            self._status[name] = status

    def server_available(self, name: str) -> bool:
        return self._status.get(name) == ServerStatus.CONNECTED

    # --- tools ---

    def register_tool(self, info: ToolInfo) -> None:
        self._tools[info.qualified] = info
        self._server_tools.setdefault(info.server, []).append(info)

    def clear_server_tools(self, server: str) -> None:
        for t in self._server_tools.pop(server, []):
            self._tools.pop(t.qualified, None)

    def get_tool(self, qualified: str) -> ToolInfo | None:
        return self._tools.get(qualified)

    def all_tools(self) -> list[ToolInfo]:
        return list(self._tools.values())

    def server_tools(self, server: str) -> list[ToolInfo]:
        return list(self._server_tools.get(server, []))

    # --- группы ---

    @property
    def groups(self) -> dict[str, GroupConfig]:
        return self._groups

    def group_tools(self, group: str) -> list[ToolInfo]:
        g = self._groups.get(group)
        if g is None or not g.enabled:
            return []
        out: list[ToolInfo] = []
        for qualified in g.tools:
            info = self._tools.get(qualified)
            if info is not None:
                out.append(info)
        return out

    def tool_groups(self, qualified: str) -> list[str]:
        return [name for name, g in self._groups.items() if g.enabled and qualified in g.tools]

    # --- фильтрация ---

    def filter_tools(self, spec: FilterSpec) -> list[ToolInfo]:
        """Возвращает tools по FilterSpec (все — даже недоступные)."""
        if spec.mode == "all":
            return self.all_tools()
        if spec.mode == "group":
            return self.group_tools(spec.group)
        if spec.mode == "single":
            info = self._tools.get(spec.tool)
            return [info] if info else []
        if spec.mode == "jwt":
            return [self._tools[q] for q in spec.tools if q in self._tools]
        return []

    def is_tool_available(self, qualified: str, spec: FilterSpec) -> bool:
        """Проверяет, что tool в скоупе и его сервер доступен."""
        if spec.mode == "single" and qualified != spec.tool:
            return False
        if spec.mode == "group":
            group = self._groups.get(spec.group)
            if group is None or not group.enabled or qualified not in group.tools:
                return False
        if spec.mode == "jwt" and qualified not in spec.tools:
            return False
        info = self._tools.get(qualified)
        if info is None:
            return False
        return self.server_available(info.server)

    def mcp_tools(self, spec: FilterSpec) -> list[MCPTool]:
        """Возвращает MCPTool-ы (для tools/list) по FilterSpec."""
        return [t.to_mcp() for t in self.filter_tools(spec)]

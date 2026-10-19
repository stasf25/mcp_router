"""Тесты registry: маппинг, группы, FilterSpec, availability."""

from __future__ import annotations

from mcp_types import Tool as MCPTool

from mcp_router.config import GroupConfig, ServerConfig
from mcp_router.registry import FilterSpec, Registry, ServerStatus, ToolInfo


def make_tool(server: str, name: str, description: str = "desc") -> ToolInfo:
    t = MCPTool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
    )
    return ToolInfo(server=server, name=name, schema=t)


def make_registry() -> Registry:
    servers = [
        ServerConfig(name="playwright", url="http://pw:3000/mcp"),
        ServerConfig(name="brightdata", url="http://bd:3000/mcp"),
    ]
    groups = [
        GroupConfig(name="web", tools=["playwright.navigate", "brightdata.scrape"], enabled=True),
        GroupConfig(name="off", tools=["playwright.click"], enabled=False),
    ]
    reg = Registry(servers, groups)
    reg.register_tool(make_tool("playwright", "navigate"))
    reg.register_tool(make_tool("playwright", "click"))
    reg.register_tool(make_tool("brightdata", "scrape"))
    # подключить оба сервера
    for s in servers:
        reg.set_status(s.name, ServerStatus.CONNECTED)
    return reg


def test_tool_qualified_name():
    reg = make_registry()
    names = {t.qualified for t in reg.all_tools()}
    assert names == {"playwright.navigate", "playwright.click", "brightdata.scrape"}


def test_filter_all():
    reg = make_registry()
    assert {t.qualified for t in reg.filter_tools(FilterSpec.all())} == {
        "playwright.navigate",
        "playwright.click",
        "brightdata.scrape",
    }


def test_filter_group():
    reg = make_registry()
    assert {t.qualified for t in reg.filter_tools(FilterSpec.for_group("web"))} == {
        "playwright.navigate",
        "brightdata.scrape",
    }


def test_filter_disabled_group_empty():
    reg = make_registry()
    assert reg.filter_tools(FilterSpec.for_group("off")) == []


def test_filter_single():
    reg = make_registry()
    assert [t.qualified for t in reg.filter_tools(FilterSpec.single("playwright.click"))] == [
        "playwright.click"
    ]


def test_filter_single_unknown():
    reg = make_registry()
    assert reg.filter_tools(FilterSpec.single("unknown.tool")) == []


def test_is_tool_available_group():
    reg = make_registry()
    assert reg.is_tool_available("playwright.navigate", FilterSpec.for_group("web")) is True
    assert reg.is_tool_available("playwright.click", FilterSpec.for_group("web")) is False


def test_is_tool_available_single():
    reg = make_registry()
    assert reg.is_tool_available("playwright.click", FilterSpec.single("playwright.click")) is True
    assert (
        reg.is_tool_available("brightdata.scrape", FilterSpec.single("playwright.click")) is False
    )


def test_is_tool_available_when_server_down():
    reg = make_registry()
    reg.set_status("brightdata", ServerStatus.DISCONNECTED)
    assert reg.is_tool_available("brightdata.scrape", FilterSpec.all()) is False
    assert reg.is_tool_available("playwright.navigate", FilterSpec.all()) is True


def test_tool_groups_membership():
    reg = make_registry()
    assert reg.tool_groups("playwright.navigate") == ["web"]
    assert reg.tool_groups("playwright.click") == []


def test_openai_format():
    reg = make_registry()
    info = reg.get_tool("playwright.navigate")
    oa = info.to_openai()
    assert oa["type"] == "function"
    assert oa["function"]["name"] == "playwright.navigate"


def test_jwt_filter():
    reg = make_registry()
    spec = FilterSpec.jwt(["playwright.navigate", "brightdata.scrape"])
    assert {t.qualified for t in reg.filter_tools(spec)} == {
        "playwright.navigate",
        "brightdata.scrape",
    }
    assert reg.is_tool_available("playwright.click", spec) is False

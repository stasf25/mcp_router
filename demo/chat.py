#!/usr/bin/env python3
"""CLI chat with an OpenAI-compatible model, using tools served by mcp_router.

The chat starts with an empty toolset. Tools are selected at runtime via the
`/select` command, which calls the mcp_router `/run/select` endpoint. The chosen
tools are passed to the model in the `tools` parameter (not in the prompt) and
stay fixed until the next `/select`. Tool invocations are executed through the
mcp_router `/run/tool/call` REST endpoint (no MCP client, no JWT).

Commands:
  /clear                       - reset the conversation context (keeps tools)
  /select *                    - select all tools (aggregator)
  /select <group>              - select tools of a group
  /select <server.tool> [...]  - select specific tools
  /help                        - show this help
  /quit, exit, Ctrl+D          - quit
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import OpenAI

MAX_LLM_ITERATIONS = 25
HELP = """\
Commands:
  /clear                       - reset the conversation context (keeps tools)
  /select *                    - select all tools (aggregator)
  /select <group>              - select tools of a group
  /select <server.tool> [...]  - select specific tools
  /help                        - show this help
  /quit, exit, Ctrl+D          - quit
"""


class ChatConfig:
    def __init__(self) -> None:
        self.router_url = os.environ["MCP_ROUTER_URL"].rstrip("/")
        self.model_base_url = os.environ.get("CHAT_MODEL_BASE_URL") or None
        self.model_name = os.environ["CHAT_MODEL_NAME"]
        self.model_api_key = os.environ.get("CHAT_MODEL_API_KEY") or "not-needed"


class Session:
    """Holds the conversation context and the currently selected tools."""

    def __init__(self) -> None:
        self.messages: list[dict] = [{"role": "system", "content": "You are a helpful assistant."}]
        self.tools: list[dict] = []  # OpenAI function definitions, empty until /select
        self.tool_names: set[str] = set()  # qualified names of the selected tools

    def clear(self) -> None:
        self.messages = [{"role": "system", "content": "You are a helpful assistant."}]

    def set_tools(self, tools: list[dict]) -> None:
        self.tools = tools
        self.tool_names = {t["function"]["name"] for t in tools}


def _tool_call_arguments(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _tool_result_text(result) -> str:
    """Flattens a /run/tool/call result into plain text for the model."""
    content = result.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text is not None:
                    parts.append(str(text))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if content is None:
        return json.dumps(result, ensure_ascii=False)
    return str(content)


def call_mcp_tool(router_url: str, name: str, arguments: dict) -> str:
    """Executes a tool via the mcp_router /run/tool/call REST endpoint."""
    url = f"{router_url}/run/tool/call"
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, json={"name": name, "arguments": arguments})
    except httpx.HTTPError as exc:
        return f"Tool call failed (network): {exc}"
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        return f"Tool call failed (HTTP {resp.status_code}): {detail}"
    body = resp.json()
    result = body.get("result", {})
    text = _tool_result_text(result)
    if result.get("isError"):
        return f"Tool returned an error:\n{text}"
    return text


def select_tools(router_url: str, spec: str) -> tuple[list[dict], int, str]:
    """Calls /run/select (or /run/select/{group}) and returns (tools, count, msg)."""
    args = spec.split()
    if not args or args[0] == "*":
        # Aggregator: all tools.
        url = f"{router_url}/run/select"
        payload: dict = {}
    elif any("." in tok for tok in args):
        # Specific tools.
        url = f"{router_url}/run/select"
        payload = {"tools": args}
    else:
        # Single non-dotted token -> group.
        group = args[0]
        url = f"{router_url}/run/select/{group}"
        payload = {}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload)
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise RuntimeError(f"select failed (HTTP {resp.status_code}): {detail}")
    body = resp.json()
    tools = body.get("tools", [])
    return tools, len(tools), f"Selected {len(tools)} tools"


def run_llm_loop(client: OpenAI, cfg: ChatConfig, session: Session, user_input: str) -> dict:
    """Runs the model loop with tool calls; returns final stats."""
    session.messages.append({"role": "user", "content": user_input})
    used_tools: list[str] = []
    final_content: str | None = None
    usage = None

    tools_param = session.tools or None

    for _ in range(MAX_LLM_ITERATIONS):
        resp = client.chat.completions.create(
            model=cfg.model_name,
            messages=session.messages,
            tools=tools_param,
            tool_choice="auto" if tools_param else None,
        )
        message = resp.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if tool_calls:
            # Persist the assistant message with tool_calls so the model sees it.
            session.messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                name = tc.function.name
                if name not in used_tools:
                    used_tools.append(name)
                arguments = _tool_call_arguments(tc.function.arguments)
                result = call_mcp_tool(cfg.router_url, name, arguments)
                session.messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )
        else:
            final_content = message.content or ""
            session.messages.append({"role": "assistant", "content": final_content})
            usage = getattr(resp, "usage", None)
            break

    return {
        "content": final_content,
        "used_tools": used_tools,
        "usage": usage,
    }


def print_stats(result: dict) -> None:
    usage = result["usage"]
    if usage is not None:
        prompt = getattr(usage, "prompt_tokens", "n/a")
        completion = getattr(usage, "completion_tokens", "n/a")
        total = getattr(usage, "total_tokens", "n/a")
    else:
        prompt = completion = total = "n/a"
    used = result["used_tools"]
    print("\n--- Stats ---")
    print(f"Prompt tokens:     {prompt}")
    print(f"Completion tokens: {completion}")
    print(f"Total tokens:      {total}")
    if used:
        print("Tools used:        " + ", ".join(used))
    else:
        print("Tools used:        none")


def main() -> int:
    cfg = ChatConfig()
    client = OpenAI(
        base_url=cfg.model_base_url,
        api_key=cfg.model_api_key,
    )
    session = Session()

    print("mcp_router demo chat")
    print("Type /help for commands, Ctrl+D or /quit to exit.\n")

    while True:
        try:
            line = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit", "exit", "quit"):
            print("Bye.")
            return 0
        if line == "/help":
            print(HELP)
            continue
        if line == "/clear":
            session.clear()
            print("Context cleared (tools kept).")
            continue
        if line.startswith("/select"):
            try:
                tools, count, msg = select_tools(cfg.router_url, line[len("/select"):].strip())
            except RuntimeError as exc:
                print(f"Error: {exc}")
                continue
            session.set_tools(tools)
            print(f"{msg}: {count}")
            continue
        if line.startswith("/"):
            print(f"Unknown command: {line} (try /help)")
            continue

        try:
            result = run_llm_loop(client, cfg, session, line)
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}")
            continue

        print(f"\nAssistant> {result['content']}")
        print_stats(result)
        print()

    return 0


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    sys.exit(main())

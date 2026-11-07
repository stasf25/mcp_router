<div align="center">

# mcp_router

**Containerized MCP orchestrator: aggregation, grouping and dynamic filtering of tools for LLMs**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP 2.3.0](https://img.shields.io/badge/MCP-2.3.0-green.svg)](https://modelcontextprotocol.io/)
[![License: PN v1.0.0](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-blue.svg)](https://polyformproject.org/licenses/noncommercial/1.0.0)
[![Tests](https://img.shields.io/badge/tests-80%20passed-brightgreen.svg)](test_suite.md)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)

</div>

A containerized MCP orchestrator: it aggregates tools from several downstream MCP servers (e.g. Playwright, BrightData and others) and exposes them to the client through a single MCP endpoint, or through a simple REST API.

The project solves two important problems for businesses and AI developers:

- **LLM token savings.** Full-featured MCP servers can expose dozens of tools, of which usually only a small subset is required by the LLM to fulfill a specific request. Passing the full set of tools with their complete JSON descriptions to the LLM leads to "bloating" the context and increased input-token consumption. This project lets you build a group of only the required tools for each LLM call and pass only those to the model.
- **REST → MCP proxying.** Direct invocation of any MCP tool with a single HTTP request (`/run/tool/call`) — without implementing an MCP client, JSON-RPC or complex handshakes. Ideal for n8n (via HTTP Request), scripts and Python agents.

Real token savings on simple requests can reach **10×** compared to passing the entire tool pool to the LLM.

---

## 📑 Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
  - [Prerequisites](#prerequisites)
  - [Step 1. Configuration setup](#step-1-configuration-setup-configyaml)
  - [Step 2. Launch](#step-2-launch)
  - [Step 3. Verification](#step-3-verification)
- [Dify Integration](#dify-integration)
- [REST API](#rest-api)
  - [Runtime (`/run/*`)](#runtime-run---for-llm-agents-and-integrations)
  - [Administration (`/api/*`)](#administration-api-read-only)
- [MCP endpoint](#mcp-endpoint)
- [Observability](#observability)
- [Fault Tolerance](#fault-tolerance)
- [Demo](#demo)
- [Testing](#testing)
- [Documentation](#documentation)
- [Limitations and decisions](#limitations-and-decisions)
- [JWT secret](#jwt-secret)
- [License](#license)

---

## Features

- Single MCP endpoint `/mcp` (Streamable HTTP / SSE), compatible with any MCP client:
  - `/mcp` — aggregate of tools from all connected MCP servers;
  - `/mcp/{group}` — tools of a specific group;
  - `/mcp/{server}.{tool}` — a specific tool of a given server.
- Static grouping of tools via a configuration file by semantic criteria: one tool can belong to several groups, and one group can contain tools from different MCP servers.
- Dynamic grouping of tools — the required grouping can be requested at runtime by sending a REST API request to `/run/select/*`:
  - `POST /run/select` — selection from all available tools;
  - `POST /run/select/{group}` — selection from the tools belonging to a group.
- Direct tool invocation via the REST API (REST → MCP proxying): `POST /run/tool/call`.
- Administrative WEB UI at `http://localhost:8000/` (default) + SWAGGER at `/docs`.
- Prometheus metrics at `/metrics`.
- Graceful degradation: when one server is unavailable, the rest keep working.
- Secret security: `config.yaml` contains only environment-variable names (`${ENV_VAR}`); the real values are substituted at startup.

---

## Architecture

```
 ┌─────────────────────────────────────────────────────────────┐
 │           Consumers (Dify / n8n / Python)                   │
 └──────────────────────────┬──────────────────────────────────┘
                            │ MCP (Streamable HTTP / SSE)
                            ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                      mcp_router                             │
 │  ┌───────────────────────────────────────────────────────┐  │
 │  │  MCP endpoint /mcp (single ScopedMCPServer):          │  │
 │  │   /mcp            — aggregate of all tools            │  │
 │  │   /mcp/{group}       — group tools                    │  │
 │  │   /mcp/{server}.{tool} — a single tool                │  │
 │  └───────────────────────────────────────────────────────┘  │
 │  ┌───────────────────────────────────────────────────────┐  │
 │  │  Tool Registry + Groups (core logic)                  │  │
 │  └──────────────────────────┬────────────────────────────┘  │
 │  ┌──────────────────────────▼────────────────────────────┐  │
 │  │  MCP-Client (connection aggregator)                   │  │
 │  │  - Connection Pool per server                         │  │
 │  │  - Circuit Breaker                                    │  │
 │  └──────────────────────────┬────────────────────────────┘  │
 │  ┌──────────────────────────▼────────────────────────────┐  │
 │  │  REST API (/api + /run) + Admin SPA                   │  │
 │  └───────────────────────────────────────────────────────┘  │
 └──────────────────────────┬──────────────────────────────────┘
                            │ docker network / HTTP
         ┌──────────────────┼──────────────────┐
         ▼                  ▼                  ▼
    Playwright-MCP    BrightData-MCP    ... others
```

**MCP compatibility:** the `/mcp` endpoint behaves as a full-featured MCP server (handshake, capabilities, `tools/list`, `tools/call`).

**Stateless:** runtime state is not mutated through the API; the service scales horizontally. Tool-selection state is not stored on the server — it lives in a signed JWT (TTL in the `exp` claim, HMAC signature). For horizontal scaling (multiple replicas) you must set a fixed `MCP_ROUTER_JWT_SECRET` — otherwise each replica generates its own random secret and won't understand the tokens of its neighbours (see [JWT secret](#jwt-secret)).

**Single `ScopedMCPServer`:** tool filtering is done per request.

**Namespacing:** uniqueness of tool names is ensured via the qualified name `server.tool`.

---

## Requirements

- Python ≥ 3.10 (`python3.10-slim` image).
- The full list of dependencies and their versions (FastAPI, uvicorn, `mcp==2.3.0`, pydantic, PyYAML, structlog, PyJWT, httpx, prometheus-client and others) is in [`pyproject.toml`](pyproject.toml) (sections `[project]` and `[project.optional-dependencies]`).
- For local development: `pytest`, `pytest-asyncio`, `ruff` (section `dev`).
- For running in Docker — Docker/Compose itself.

---

## Quick Start

### Prerequisites

- **Docker & Docker Compose** (recommended) **or** Python ≥ 3.10 for a local build.
- Access to MCP servers (Playwright, BrightData and others) — locally in a docker network or over HTTP(S).
- A chat-model API key for the demo app (OpenAI-compatible).

### Step 1. Configuration setup (`config.yaml`)

Secrets are provided via `${ENV_VAR}` substitution — key-value pairs are stored in the `.env` file at the project root.

```yaml
servers:
   - name: playwright
     url: http://playwright:8931/mcp
     enabled: true
     timeout: 30
   - name: brightdata
     url: https://mcp.brightdata.com/mcp
     headers:
       - Authorization: Bearer ${BRIGHTDATA_TOKEN}
     enabled: true
     timeout: 30

groups:
   - name: web_scraping
     tools:
       - playwright.browser_navigate
       - playwright.browser_click
       - brightdata.scrape_as_html
     enabled: true
```

Authentication headers are set in the `headers` section as a list of `Header: value` pairs — this lets you pass the key in different ways (`Authorization: Bearer ...`, `X-API-Key: ...`, a token in the URL query parameter, etc.).

**Configuration field semantics:**

- `${ENV_VAR}` substitution works in all string values of `config.yaml` (in `url`, `headers`, `name` and `timeout`). Key-value pairs are stored in the `.env` file at the project root (see [`.env.example`](.env.example)). If an environment variable is not set — the app fails fast at startup with a clear error.
- `enabled: true` — the server is connected, its tools are registered and visible; with `enabled: false` the server/group is ignored (not connected, tools not visible).
- `timeout: 30` — tool-call timeout in seconds (default 30).
- The transport to the downstream server is determined by `url`: a path ending in `/sse` means SSE; otherwise — streamable HTTP. Example: `https://host/mcp` → streamable HTTP; `https://host/sse` → SSE.

### Step 2. Launch

#### Option A: Docker (recommended)

```bash
docker compose up --build
```

#### Option B: Local launch (for developers)

Python and library version requirements are in [`pyproject.toml`](pyproject.toml) (`Python ≥ 3.10`).

```bash
pip install -e .                # or: pip install .
uvicorn mcp_router.main:create_app --factory --host 0.0.0.0 --port 8000
```

### Step 3. Verification

```bash
curl http://localhost:8000/api/health          # {"status": "ok"} or "degraded"
curl http://localhost:8000/api/tools           # all known tools
curl http://localhost:8000/metrics             # Prometheus metrics
```

### Docker / docker-compose

```yaml
services:
   mcp_router:
     build: .
     ports:
       - "127.0.0.1:8000:8000"      # localhost only
     env_file:
       - .env
     volumes:
       - ./config.yaml:/app/config.yaml:ro
     networks:
       - mcp_net
     healthcheck:
       test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
       interval: 30s
       timeout: 5s
       retries: 3
   playwright:
     image: mcp/playwright:latest
     ...
     networks:
       - mcp_net

networks:
   mcp_net:
     driver: bridge
```

The deployment model is «one container per MCP server + orchestrator» in a single docker network; external MCP servers can connect over HTTP(S).

**Joining an external (shared) docker network:** so that other containers on the host (e.g. Dify) can reach `mcp_router` by service name, you can attach it to a shared docker network. To do so, set `SHARED_NETWORK=true` (default `false`) in `.env` and create the network in advance:

```
docker network create shared-network
```

`docker-compose.yml` declares `shared-network` as `external: ${SHARED_NETWORK:-false}` — when `true` it is attached as external, when `false` (default) it is ignored. Make sure the network exists before startup, otherwise Compose will fail with an error.

---

## Dify Integration

The core idea — **attach both `mcp_router` and the Dify containers that must reach it to a single external docker network**.

All MCP requests in Dify go **through its own SSRF proxy** (squid container). So you need to attach not only `mcp_router` to the network, but also the SSRF proxy — plus allow the proxy to reach the private address of `mcp_router`.

Setup order:

1. **Create the shared network** (once):

   ```
   docker network create shared-network
   ```

2. **mcp_router** — enable joining the network in `.env`:

   ```
   SHARED_NETWORK=true
   ```

   (see the «Docker / docker-compose» section above; the network must exist before startup).

3. **Dify** — in `docker-compose.yml`:

   - declare the network as external:

     ```yaml
     networks:
       shared-network:
         external: true
     ```

   - attach the `ssrf_proxy` and `agent_ssrf_proxy` services to it (add `shared-network` to their `networks` list);

4. **Dify** — in `.env`:

   - allow the proxy to reach `mcp_router` (this is a private address that squid blocks by default):

     ```
     SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=mcp_router
     ```


After that you can add an MCP server in Dify and connect to it. The URL can point either to the aggregate of all tools (`http://mcp_router:8000/mcp/`) or to a specific group (`http://mcp_router:8000/mcp/{group}`) — the tool set is determined by the path (see «MCP endpoint»). You can create a separate MCP server per group.

### Troubleshooting common errors

| Error | Where it occurs | Cause | Solution |
| --- | --- | --- | --- |
| `503 Service Unavailable` | `ssrf_proxy` | the proxy has no network route to `mcp_router` | attach `ssrf_proxy` to `shared-network` |
| `403` | `ssrf_proxy` (squid) | private-address blocking `deny to_private_networks` | set `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=mcp_router` |
| `421 Misdirected Request` | `mcp_router` | unknown `Host` header | see «Allowed Host headers» in the «MCP endpoint» section |

---

## REST API

### Runtime (`/run/*`) — for LLM agents and integrations

#### Dynamic tool selection → `POST /run/select`

Issues a signed JWT for a subset of tools and returns them in OpenAI function-calling format — you can pass them directly to the LLM API without an MCP client.

```bash
curl -X POST http://localhost:8000/run/select \
  -H 'Content-Type: application/json' \
  -d '{"tools": ["playwright.browser_navigate", "playwright.browser_click"], "ttl": 3600}'
```

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_at": 1762212000,
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "playwright.browser_navigate",
        "description": "...",
        "parameters": { "type": "object", "properties": { ... } }
      }
    },
    ...
  ]
}
```

`expires_at` — Unix timestamp of token expiry, computed from `ttl`.

**Semantics:**

- empty/missing `tools` — aggregate mode (all tools of the registry);
- tools not found in the registry → `400{"detail": "Tools not found: [...]"}`;
- server unavailable → `400{"detail": "Tools unavailable: [...]"}`.

`POST /run/select/{group}` — the same, but limited to the group's composition (non-existent group → `404`; empty `tools` → the whole group).

#### REST → MCP proxying

A key feature for integrations. Direct invocation of any tool with a single HTTP request — without an MCP client, JSON-RPC or handshake.

```bash
curl -X POST http://localhost:8000/run/tool/call \
  -H 'Content-Type: application/json' \
  -d '{"name": "playwright.browser_click", "arguments": {"selector": "#submit"}}'
```

The response format is equivalent to the standard MCP `tools/call` response:

```json
{
  "status": "success",
  "result": {
    "content": [{ "type": "text", "text": "Button clicked successfully" }],
    "isError": false
  }
}
```

**Why it's needed:** it lets you build LLM `tool_calls` loops without having to write an MCP client. A usage demonstration — see the [Demo](#demo) section.

**Error format.** The endpoint always answers `200 OK` — both on success and on an invalid/unavailable tool. The error is passed inside the result via `isError: true` and a human-readable `content`. This is intentional: the LLM `tool_calls` loop does not fail on an HTTP error, and the model receives an explanation and can react (for example, ask to choose another tool).

```json
{
  "status": "success",
  "result": {
    "content": [{ "type": "text", "text": "Tool 'playwright.browser_click' returned an error: ..." }],
    "isError": true
  }
}
```

HTTP-level errors (400/404/500) for `/run/*` use the standard FastAPI format `{"detail": "..."}` and occur only on request validation (for example, an unknown group in `/run/select/{group}` — `404`).

### Administration (`/api/*`, read-only)

| Endpoint | Description |
| --- | --- |
| `GET /api/health` | Health check (liveness + readiness) |
| `GET /api/servers` | List of servers with status (`connected` / `disconnected` / `error`) |
| `GET /api/servers/{name}/list` | Tools of a specific server |
| `GET /api/groups` | List of groups |
| `GET /api/groups/{name}/list` | Group tools with server and status |
| `GET /api/tools` | All known tools (server, status, groups) |
| `GET /api/tool/{server}.{tool}` | Full info on a tool (including `input_schema`) |
| `GET /` | Admin SPA (minimal UI over the REST API) |

`GET /api/health`: `{"status": "ok"}` — all enabled servers are connected (or there are none); `{"status": "degraded"}` — some servers are unavailable (graceful degradation). In both cases HTTP 200 is returned (not 503), so `curl -f` in the docker-compose healthcheck does not consider the container unhealthy during a temporary unavailability of a single server.

---

## MCP endpoint

A single `ScopedMCPServer` is mounted at `/mcp`; the tool set is determined per request from the path. Compatible with any MCP client (Dify, n8n, Python MCP SDK).

| Path | Behavior |
| --- | --- |
| `/mcp` | MCP server with **all** tools |
| `/mcp/{group}` | MCP server with the tools of the given group |
| `/mcp/{server}.{tool}` | MCP server with a single tool |

**Subpath resolution rule:** if the segment after `/mcp/` contains a dot (`.`), it is treated as `server.tool`; otherwise — as a group name. Dots are not allowed in server and group names.

**Allowed Host headers:** on `/mcp` the MCP SDK's DNS-rebinding protection is enabled (it is enabled automatically because the default host is `127.0.0.1`). By default the SDK allows only localhost hosts, so the list is extended for the docker network — `Host: localhost`, `Host: mcp_router` and `127.0.0.1` are accepted (with or without a port). An unknown `Host` is rejected with HTTP `421 Misdirected Request`. If a client reaches `/mcp` under another name (e.g. `mcp_router.myapp`), add it to `allowed_hosts` in `build_mcp_app()` (`src/mcp_router/mcp_server.py`).

**JWT filtering:** pass the token from `/run/select` in `Authorization: Bearer <jwt>` or query `?token=<jwt>` — `/mcp` will return only the tools from the token (in both `tools/list` and `tools/call`).

**Behavior of `/mcp` with a JWT:**

- no JWT passed — aggregate mode (all tools);
- invalid/expired JWT — a safe empty scope: `tools/list` returns an empty array, `tools/call` returns an error for any tool (tool out of scope).

---

## Observability

- Structured JSON logs (`structlog`): `tool.call`, `server.health`, `group.select`.
- Prometheus at `GET /metrics` (no authentication):
  - `mcp_router_tool_calls_total{server, tool, status}` — tool-call counter, `status` = `ok` / `error` (covers both `/run/tool/call` and MCP `tools/call`);
  - `mcp_router_tool_calls_duration_seconds{server, tool}` — duration histogram;
  - `mcp_router_server_up{server}` — `1` (connected) / `0`;
  - `mcp_router_server_status{server}` — `1`=connected, `2`=disconnected, `3`=error;
  - `mcp_router_server_tools_total{server}` — number of registered tools;
  - `mcp_router_http_requests_total` + `mcp_router_http_request_duration_seconds` — HTTP metrics with normalization of dynamic paths.

> ⚠️ **Caution (deployment):** `/metrics` is served without authentication. In docker-compose the port is bound to `127.0.0.1` — do not publish it externally if you do not want to expose metrics and server names.

---

## Fault Tolerance

- Circuit breaker on each MCP-server connection. Parameters are configured **per-server** in `config.yaml` (default values: `threshold=5` errors per `window=60s`, recovery after `recovery=30s`):

  ```yaml
  servers:
    - name: playwright
      url: http://playwright:8931/mcp
      circuit_breaker:
        threshold: 5      # errors per window to open
        window: 60        # window, sec
        recovery: 30      # time to half-open, sec
  ```

- When a server is unavailable, its tools are marked unavailable but not removed from the registry — the other servers keep working (graceful degradation).

---

## Demo

`demo/` contains a standalone CLI app `chat.py` — a ready-made example of using mcp_router from a Python agent: tool selection via `/run/select` and invocations via `/run/tool/call`, working with any OpenAI-compatible model.

This is exactly where both project values are clearly visible:

- **Token savings** — start with an empty tool set, fill it only through `/select`;
- **REST → MCP proxying** — tools are executed via `/run/tool/call`, without an MCP client.

### Demo features

- start with an empty tool set; fill it only through `/select`;
- tools are passed to the model in the `tools` parameter (not in the prompt) and stay unchanged until the next `/select`;
- after each response — token statistics (`usage`) and the list of actually used tools;
- commands: `/select * | group | server.tool ...`, `/clear` (resets context, tools are kept), `/help`, `/quit`/`exit`/Ctrl+D.

### Running the demo

Run mcp_router in a separate terminal window:

```bash
docker compose up --build
```

Then:

```bash
pip install -r demo/requirements.txt
cp demo/.env.example demo/.env     # fill in the parameters
python demo/chat.py
```

`demo/.env` parameters:

| Variable | Purpose |
| --- | --- |
| `MCP_ROUTER_URL` | Base URL of mcp_router (e.g. `http://localhost:8000/`) |
| `CHAT_MODEL_BASE_URL` | Chat-model endpoint, OpenAI-compatible (e.g. `https://api.openai.com/v1`) |
| `CHAT_MODEL_NAME` | Model name (e.g. `gpt-4o-mini`) |
| `CHAT_MODEL_API_KEY` | Chat-model API key |

### Example session

```
You> /select web_scraping
Selected 3 tools: 3

You> Return the content of the page https://example.com in md format

Assistant> The page has been loaded. Let's look at its content. The page
https://example.com is the standard IANA demo page.
Here is its content in Markdown format:

---
# Example Domain
This domain is for use in illustrative examples in documents. You may use
this domain in literature without prior coordination or asking for permission.

More information...
---

--- Stats ---
Prompt tokens:     1229
Completion tokens: 81
Total tokens:      1310
Tools used:        playwright.browser_navigate, brightdata.scrape_as_html
```

---

## Testing

Full description is in [`test_suite.md`](test_suite.md). In brief:

```bash
source venv            # activate the environment (created via `source make_venv`)
python -m pytest -q    # 78 passed
ruff check src tests   # linter — clean
```

**Test levels:**

- **Unit** — `config` (including `${ENV_VAR}` resolution), `registry`, JWT.
- **API** — all REST endpoints `/api/*`.
- **Contract** — full MCP protocol through the external `Client` (mcp SDK): handshake, `tools/list` on `/mcp`/`/mcp/{group}`/`/mcp/{server}.{tool}`, `tools/call`.
- **Run (stage 3)** — JWT, `/run/select`, `/run/select/{group}`, `/run/tool/call`, JWT filtering on `/mcp` via header and query.
- **Metrics** — `/metrics`, tool-call counters, status gauges, path normalization.

---

## Documentation

| Document | Purpose |
| --- | --- |
| [`test_suite.md`](test_suite.md) | Description of the test suite |

---

## Limitations and decisions

- MCP SDK is pinned: `mcp==2.3.0` (v2 interface, `MCPServer`).
- The transport to downstream servers is determined by URL (see [Configuration](#step-1-configuration-setup-configyaml)).

---

## JWT secret

Token signature — HMAC-SHA256. The secret is taken from the env `MCP_ROUTER_JWT_SECRET`; if it is not set, the service generates a random secret at startup (valid for a single instance, but not for scaling).

For **multi-replica** (horizontal scaling) you must set a fixed `MCP_ROUTER_JWT_SECRET` — otherwise each replica signs tokens with its own random secret and cannot verify the tokens of the neighbouring replicas.

**Ways to generate a strong secret (≥ 32 bytes):**

```bash
openssl rand -base64 48
# or
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Example usage (in `.env`):**

```ini
MCP_ROUTER_JWT_SECRET=Your-very-very-strong-JWT-secret
```

---

## LICENSE

This project is licensed under the **PolyForm Noncommercial License 1.0.0**.

You may use, modify, and distribute this software for any **noncommercial purpose** (including personal study, research, hobby projects, and use by noncommercial organizations) in accordance with the full license terms.

**Commercial use requires prior written consent.** If you wish to use this software for any commercial purpose—including but not limited to running it in a for-profit environment, incorporating it into a paid product or service, or using it to generate revenue—you must obtain an explicit, written commercial license from the author.

For the complete terms and conditions, please refer to the official license text:  
[https://polyformproject.org/licenses/noncommercial/1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)

A copy of the full license text is also included in the [`LICENSE`](./LICENSE) file in the root of this repository.

---

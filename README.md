<div align="center">

# mcp_router

**Контейнеризированный MCP-оркестратор: агрегация, группировка и динамическая фильтрация tools для LLM**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP 2.3.0](https://img.shields.io/badge/MCP-2.3.0-green.svg)](https://modelcontextprotocol.io/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Tests](https://img.shields.io/badge/tests-78%20passed-brightgreen.svg)](test_suite.md)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)

</div>

Контейнеризированный MCP-оркестратор: агрегирует tools из нескольких нижестоящих MCP-серверов (например: Playwright, BrightData и др.) и отдаёт их клиенту через единый MCP-endpoint, либо через простой REST API.

Проект решает две важные задачи для бизнеса и AI-разработчиков:

- **Экономия токенов LLM.** Полнофункциональные mcp-серверы могут экспонировать десятки инструментов, из которых, обычно, лишь малая часть требуется LLM для выполнения конкретного запроса. Передача в LLM полного набора инструментов с их полными json-описаниями приводит к "раздуванию" контекста и повышенному расходу входных токенов. Данный проект позволяет для каждого вызова LLM сформировать группу только необходимых tools и передать модели только их.
- **Проксирование REST → MCP.** Прямой вызов любого MCP-инструмента одним HTTP-запросом (`/run/tool/call`) — без реализации MCP-клиента, JSON-RPC и сложных handshake. Идеально для n8n (через HTTP Request), скриптов и Python-агентов.

Реальная экономия токенов на простых запросах может достигать **10 раз** по сравнению с передачей LLM всего пула tools.

---

## 📑 Содержание

- [Возможности](#возможности)
- [Архитектура](#архитектура)
- [Требования](#требования)
- [Быстрый старт](#быстрый-старт)
  - [Предварительные требования](#предварительные-требования)
  - [Шаг 1. Подготовка конфигурации](#шаг-1-подготовка-конфигурации-configyaml)
  - [Шаг 2. Запуск](#шаг-2-запуск)
  - [Шаг 3. Проверка](#шаг-3-проверка)
- [REST API](#rest-api)
  - [Runtime (`/run/*`)](#runtime-run---для-llm-агентов-и-интеграций)
  - [Администрирование (`/api/*`)](#администрирование-api-read-only)
- [MCP-endpoint](#mcp-endpoint)
- [Наблюдаемость](#наблюдаемость)
- [Отказоустойчивость](#отказоустойчивость)
- [Demo](#demo)
- [Тестирование](#тестирование)
- [Документация](#документация)
- [Ограничения и решения](#ограничения-и-решения)
- [JWT-секрет](#jwt-секрет)
- [License](#license)

---

## Возможности

- Единый MCP-endpoint `/mcp` (Streamable HTTP / SSE), совместимый с любым MCP-клиентом:
  - `/mcp` — агрегатор tools со всех подключенных mcp-серверов;
  - `/mcp/{group}` — tools конкретной группы;
  - `/mcp/{server}.{tool}` — конкретный tool заданного сервера.
- Статическая группировка tools через файл конфигурации по смысловым признакам: один tool может входить в несколько групп, в одну группу могут входить tools с различных mcp-серверов.
- Динамическая группировка tools — нужная группировка tools может быть запрошена в runtime путем направления REST API запроса на `/run/select/*`:
  - `POST /run/select` — выбор из всех доступных tools;
  - `POST /run/select/{group}` — выбор из tools, входящих в группу.
- Прямой вызов инструмента через REST API (проксирование REST → MCP): `POST /run/tool/call`.
- Административный WEB-интерфейс на `http://localhost:8000/` (по умолчанию) + SWAGGER на `/docs`.
- Prometheus-метрики на `/metrics`.
- Graceful degradation: при недоступности одного сервера остальные продолжают работать.
- Безопасность секретов: в `config.yaml` — только имена переменных окружения (`${ENV_VAR}`), реальные значения подставляются при старте.

---

## Архитектура

```
 ┌─────────────────────────────────────────────────────────────┐
 │           Потребители (Dify / n8n / Python)                 │
 └──────────────────────────┬──────────────────────────────────┘
                            │ MCP (Streamable HTTP / SSE)
                            ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                      mcp_router                             │
 │  ┌───────────────────────────────────────────────────────┐  │
 │  │  MCP-endpoint /mcp (единый ScopedMCPServer):          │  │
 │  │   /mcp            — агрегатор всех tools              │  │
 │  │   /mcp/{group}       — tools группы                   │  │
 │  │   /mcp/{server}.{tool} — один tool                    │  │
 │  └───────────────────────────────────────────────────────┘  │
 │  ┌───────────────────────────────────────────────────────┐  │
 │  │  Tool Registry + Groups (центральная логика)          │  │
 │  └──────────────────────────┬────────────────────────────┘  │
 │  ┌──────────────────────────▼────────────────────────────┐  │
 │  │  MCP-Client (агрегатор подключений)                   │  │
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
    Playwright-MCP    BrightData-MCP    ... другие
```

**MCP-совместимость:** эндпоинт `/mcp` ведёт себя как полноценный MCP-сервер (handshake, capabilities, `tools/list`, `tools/call`).

**Stateless:** runtime-состояние не меняется через API; сервис масштабируется горизонтально. Состояние выбора tools не хранится на сервере — оно живёт в подписанном JWT (TTL в claim `exp`, подпись HMAC). Для горизонтального масштабирования (несколько реплик) обязательно задать фиксированный `MCP_ROUTER_JWT_SECRET` — иначе каждая реплика сгенерирует свой случайный секрет и не поймёт токены соседних (см. [JWT-секрет](#jwt-секрет)).

**Единый `ScopedMCPServer`:** фильтрация tools осуществляется на каждый запрос.

**Namespacing:** уникальность имен tools обеспечивается за счет qualified-имени `server.tool`.

---

## Требования

- Python ≥ 3.10 (образ `python3.10-slim`).
- Полный список зависимостей и их версий (FastAPI, uvicorn, `mcp==2.3.0`, pydantic, PyYAML, structlog, PyJWT, httpx, prometheus-client и др.) — в [`pyproject.toml`](pyproject.toml) (секции `[project]` и `[project.optional-dependencies]`).
- Для локальной разработки: `pytest`, `pytest-asyncio`, `ruff` (секция `dev`).
- Для запуска в Docker — сам Docker/Compose.

---

## Быстрый старт

### Предварительные требования

- **Docker & Docker Compose** (рекомендуется) **или** Python ≥ 3.10 для локальной сборки.
- Доступ к MCP-серверам (Playwright, BrightData и др.) — локально в docker-сети или по HTTP(S).
- API-ключ чат-модели для демо-приложения (OpenAI-совместимый).

### Шаг 1. Подготовка конфигурации (`config.yaml`)

Секреты задаются через `${ENV_VAR}`-подстановку — пары ключ-значение хранятся в файле `.env` в корне проекта.

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

Заголовки аутентификации задаются в секции `headers` как список пар `Заголовок: значение` — это позволяет передавать ключ разными способами (`Authorization: Bearer ...`, `X-API-Key: ...`, токен в query-параметре URL и т.д.).

**Семантика полей конфигурации:**

- `${ENV_VAR}`-подстановка работает во всех строковых значениях `config.yaml` (и в `url`, и в `headers`, и в `name`, и в `timeout`). Пары ключ-значение хранятся в файле `.env` в корне проекта (см. [`.env.example`](.env.example)). Если переменная окружения не задана — приложение падает при старте (fail fast) с понятной ошибкой.
- `enabled: true` — сервер подключается, его tools регистрируются и видны; при `enabled: false` сервер/группа игнорируются (не подключаются, tools не видны).
- `timeout: 30` — таймаут вызова tool в секундах (дефолт 30).
- Транспорт к нижестоящему серверу определяется по `url`: путь, оканчивающийся на `/sse`, — SSE; в остальных случаях — streamable HTTP. Пример: `https://host/mcp` → streamable HTTP; `https://host/sse` → SSE.

### Шаг 2. Запуск

#### Вариант A: Docker (рекомендуется)

```bash
docker compose up --build
```

#### Вариант B: Локальный запуск (для разработчиков)

Требования к версиям Python и библиотек — в [`pyproject.toml`](pyproject.toml) (`Python ≥ 3.10`).

```bash
pip install -e .                # или: pip install .
uvicorn mcp_router.main:create_app --factory --host 0.0.0.0 --port 8000
```

### Шаг 3. Проверка

```bash
curl http://localhost:8000/api/health          # {"status": "ok"} или "degraded"
curl http://localhost:8000/api/tools           # все известные tools
curl http://localhost:8000/metrics             # Prometheus-метрики
```

### Docker / docker-compose

```yaml
services:
   mcp_router:
     build: .
     ports:
       - "127.0.0.1:8000:8000"      # только localhost
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

Модель развёртывания — «один контейнер на MCP-сервер + оркестратор» в одной docker-сети; внешние MCP-серверы могут подключаться по HTTP(S).

**Подключение к внешней (shared) docker-сети:** чтобы другие контейнеры на хосте (например, Dify) могли обращаться к `mcp_router` по имени сервиса, можно прикрепить его к общей docker-сети. Для этого в `.env` задайте `SHARED_NETWORK=true` (по умолчанию `false`) и предварительно создайте сеть:

```
docker network create shared-network
```

`docker-compose.yml` объявляет `shared-network` как `external: ${SHARED_NETWORK:-false}` — при `true` она подключается как внешняя, при `false` (по умолчанию) игнорируется. Убедитесь, что сеть создана до запуска, иначе Compose завершится с ошибкой.

---

## REST API

### Runtime (`/run/*`) — для LLM-агентов и интеграций

#### Динамический выбор tools → `POST /run/select`

Выпускает подписанный JWT на подмножество tools и возвращает их в формате OpenAI function calling — их можно передавать напрямую в LLM API без MCP-клиента.

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

`expires_at` — Unix-время истечения токена, вычисляется из `ttl`.

**Семантика:**

- пустой/отсутствующий `tools` — режим агрегатора (все tools реестра);
- tools не найдены в реестре → `400{"detail": "Tools не найдены: [...]"}`;
- сервер недоступен → `400{"detail": "Tools недоступны: [...]"}`.

`POST /run/select/{group}` — то же самое, но ограничено составом группы (несуществующая группа → `404`; пустой `tools` → вся группа целиком).

#### Проксирование REST → MCP

Ключевая фича для интеграций. Прямой вызов любого инструмента одним HTTP-запросом — без MCP-клиента, JSON-RPC и handshake.

```bash
curl -X POST http://localhost:8000/run/tool/call \
  -H 'Content-Type: application/json' \
  -d '{"name": "playwright.browser_click", "arguments": {"selector": "#submit"}}'
```

Формат ответа эквивалентен стандартному ответу MCP `tools/call`:

```json
{
  "status": "success",
  "result": {
    "content": [{ "type": "text", "text": "Button clicked successfully" }],
    "isError": false
  }
}
```

**Зачем это нужно:** это позволяет строить циклы LLM `tool_calls` без необходимости писать MCP-клиента. Демонстрация использования — см. раздел [Demo](#demo).

**Формат ошибок.** Эндпоинт всегда отвечает `200 OK` — как при успехе, так и при некорректном/недоступном tool. Ошибка передаётся внутри результата через `isError: true` и человекочитаемый `content`. Это сделано намеренно: цикл LLM `tool_calls` не падает на HTTP-ошибке, а модель получает объяснение и может отреагировать (например, попросить выбрать другой tool).

```json
{
  "status": "success",
  "result": {
    "content": [{ "type": "text", "text": "Tool 'playwright.browser_click' вернул ошибку: ..." }],
    "isError": true
  }
}
```

Ошибки HTTP-уровня (400/404/500) для `/run/*` используют стандартный FastAPI-формат `{"detail": "..."}` и возникают только на валидации запроса (например, неизвестная группа в `/run/select/{group}` — `404`).

### Администрирование (`/api/*`, read-only)

| Endpoint | Описание |
| --- | --- |
| `GET /api/health` | Health check (liveness + readiness) |
| `GET /api/servers` | Список серверов со статусом (`connected` / `disconnected` / `error`) |
| `GET /api/servers/{name}/list` | Tools конкретного сервера |
| `GET /api/groups` | Список групп |
| `GET /api/groups/{name}/list` | Tools группы с указанием сервера и статуса |
| `GET /api/tools` | Все известные tools (сервер, статус, группы) |
| `GET /api/tool/{server}.{tool}` | Полная информация по tool'у (включая `input_schema`) |
| `GET /` | Admin SPA (минимальный UI поверх REST API) |

`GET /api/health`: `{"status": "ok"}` — все enabled-серверы подключены (или их нет); `{"status": "degraded"}` — часть серверов недоступна (graceful degradation). В обоих случаях возвращается HTTP 200 (не 503), поэтому `curl -f` в docker-compose healthcheck не считает контейнер нездоровым при временной недоступности одного сервера.

---

## MCP-endpoint

Единый `ScopedMCPServer` монтируется на `/mcp`; набор tools определяется на каждый запрос из пути. Совместим с любым MCP-клиентом (Dify, n8n, Python MCP SDK).

| Path | Поведение |
| --- | --- |
| `/mcp` | MCP-сервер со **всеми** tools |
| `/mcp/{group}` | MCP-сервер с tools указанной группы |
| `/mcp/{server}.{tool}` | MCP-сервер с единственным tool |

**Правило разрешения подпути:** если после `/mcp/` в сегменте есть точка (`.`), он трактуется как `server.tool`; иначе — как имя группы. Точка не допускается в именах серверов и групп.

**Разрешённые Host-заголовки:** на `/mcp` включена DNS-rebinding-защита MCP SDK (включается автоматически, т.к. host по умолчанию `127.0.0.1`). По умолчанию SDK пропускает только localhost-хосты, поэтому список расширен под docker-сеть — принимаются `Host: localhost`, `Host: mcp_router` и `127.0.0.1` (как с портом, так и без). Посторонний `Host` отклоняется HTTP `421 Misdirected Request`. Если клиент ходит на `/mcp` по другому имени (напр. `mcp_router.myapp`), добавьте его в `allowed_hosts` в `build_mcp_app()` (`src/mcp_router/mcp_server.py`).

**JWT-фильтрация:** передайте токен из `/run/select` в `Authorization: Bearer <jwt>` или query `?token=<jwt>` — `/mcp` вернёт только tools из токена (и в `tools/list`, и в `tools/call`).

**Поведение `/mcp` при JWT:**

- JWT не передан — режим агрегатора (все tools);
- невалидный/просроченный JWT — безопасный пустой скоуп: `tools/list` вернёт пустой массив, `tools/call` для любого tool — ошибку (tool вне скоупа).

---

## Наблюдаемость

- Структурированные JSON-логи (`structlog`): `tool.call`, `server.health`, `group.select`.
- Prometheus на `GET /metrics` (без аутентификации):
  - `mcp_router_tool_calls_total{server, tool, status}` — счётчик вызовов tools, `status` = `ok` / `error` (покрывает и `/run/tool/call`, и MCP `tools/call`);
  - `mcp_router_tool_calls_duration_seconds{server, tool}` — гистограмма длительности;
  - `mcp_router_server_up{server}` — `1` (connected) / `0`;
  - `mcp_router_server_status{server}` — `1`=connected, `2`=disconnected, `3`=error;
  - `mcp_router_server_tools_total{server}` — число зарегистрированных tools;
  - `mcp_router_http_requests_total` + `mcp_router_http_request_duration_seconds` — HTTP-метрики с нормализацией динамических путей.

> ⚠️ **Внимание (деплой):** `/metrics` отдаётся без аутентификации. В docker-compose порт биндится на `127.0.0.1` — не публикуйте наружу, если не хотите раскрывать метрики и имена серверов.

---

## Отказоустойчивость

- Circuit breaker на каждое подключение к MCP-серверу. Параметры настраиваются **per-server** в `config.yaml` (значения по умолчанию: `threshold=5` ошибок за окно `window=60s`, восстановление через `recovery=30s`):

  ```yaml
  servers:
    - name: playwright
      url: http://playwright:8931/mcp
      circuit_breaker:
        threshold: 5      # ошибок за окно, чтобы разомкнуть
        window: 60        # окно, сек
        recovery: 30      # время до half-open, сек
  ```

- При недоступности сервера его tools помечаются недоступными, но не удаляются из реестра — остальные серверы продолжают работать (graceful degradation).

---

## Demo

В `demo/` лежит автономное CLI-приложение `chat.py` — готовый пример того, как использовать mcp_router из Python-агента: выбор tools через `/run/select` и вызовы через `/run/tool/call`, работа с любой OpenAI-совместимой моделью.

Именно здесь наглядно видно обе ценности проекта:

- **Экономия токенов** — старт с пустым набором tools, наполнение только через `/select`;
- **Проксирование REST → MCP** — tools исполняются через `/run/tool/call`, без MCP-клиента.

### Возможности demo

- старт с пустым набором tools; наполнение только через `/select`;
- tools передаются модели в параметре `tools` (не в промпт) и неизменны до следующего `/select`;
- после каждого ответа — статистика токенов (`usage`) и список реально использованных tools;
- команды: `/select * | group | server.tool ...`, `/clear` (сброс контекста, tools сохраняются), `/help`, `/quit`/`exit`/Ctrl+D.

### Запуск demo

Запустите mcp_router в отдельном окне терминала:

```bash
docker compose up --build
```

Далее:

```bash
pip install -r demo/requirements.txt
cp demo/.env.example demo/.env     # заполнить параметры
python demo/chat.py
```

Параметры `demo/.env`:

| Переменная | Назначение |
| --- | --- |
| `MCP_ROUTER_URL` | Базовый URL mcp_router (например `http://localhost:8000/`) |
| `CHAT_MODEL_BASE_URL` | Эндпоинт чат-модели, OpenAI-совместимый (например `https://api.openai.com/v1`) |
| `CHAT_MODEL_NAME` | Имя модели (например `gpt-4o-mini`) |
| `CHAT_MODEL_API_KEY` | API-ключ чат-модели |

### Пример сессии

```
You> /select web_scraping
Selected 3 tools: 3

You> Выдай содержимое страницы https://example.com в формате md

Assistant> Страница загружена. Давайте посмотрим на её содержимое. Страница
https://example.com — это стандартная демонстрационная страница IANA.
Вот её содержимое в формате Markdown:

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

## Тестирование

Полное описание — в [`test_suite.md`](test_suite.md). Кратко:

```bash
source venv            # активация окружения (создаётся через `source make_venv`)
python -m pytest -q    # 78 passed
ruff check src tests   # линтер — чисто
```

**Уровни тестов:**

- **Unit** — `config` (включая `${ENV_VAR}`-резолвинг), `registry`, JWT.
- **API** — все REST-эндпоинты `/api/*`.
- **Контрактные** — полный MCP-протокол через внешний `Client` (mcp SDK): handshake, `tools/list` на `/mcp`/`/mcp/{group}`/`/mcp/{server}.{tool}`, `tools/call`.
- **Run (этап 3)** — JWT, `/run/select`, `/run/select/{group}`, `/run/tool/call`, JWT-фильтрация на `/mcp` через header и query.
- **Метрики** — `/metrics`, счётчики вызовов tools, gauge статусов, нормализация путей.

---

## Документация

| Документ | Назначение |
| --- | --- |
| [`test_suite.md`](test_suite.md) | Описание тестового набора |

---

## Ограничения и решения

- MCP SDK зафиксирован: `mcp==2.3.0` (интерфейс v2, `MCPServer`).
- Транспорт к нижестоящим серверам определяется по URL (см. [Конфигурация](#шаг-1-подготовка-конфигурации-configyaml)).

---

## JWT-секрет

Подпись токенов — HMAC-SHA256. Секрет берётся из env `MCP_ROUTER_JWT_SECRET`; если он не задан, сервис генерирует случайный секрет при старте (валидно для одного инстанса, но не для масштабирования).

Для **multi-replica** (горизонтальное масштабирование) обязательно задать фиксированный `MCP_ROUTER_JWT_SECRET` — иначе каждая реплика подпишет токены своим случайным секретом и не сможет проверить токены соседних реплик.

**Способы генерации надёжного секрета (≥ 32 байта):**

```bash
openssl rand -base64 48
# или
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Пример использования (в `.env`):**

```ini
MCP_ROUTER_JWT_SECRET=Your-very-very-strong-JWT-secretc
```

---

## License

This project is licensed under the **GNU General Public License v3.0**.

Copyright (C) 2026 mcp_router contributors

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>.

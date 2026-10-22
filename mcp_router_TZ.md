
# Техническое задание: mcp_router

## 1. Введение

### 1.1. Назначение
`mcp_router` — контейнеризированный MCP-оркестратор, выступающий одновременно:
- **MCP-клиентом** по отношению к нижестоящим MCP-серверам (Playwright, BrightData и др.)
- **MCP-сервером** по отношению к вышестоящим потребителям (Dify, n8n, Python-агенты)

### 1.2. Ключевая ценность
Решение проблемы «раздувания» контекста LLM при большом количестве доступных инструментов за счёт:
- Группировки tools по смысловым признакам
- Единого MCP-endpoint `/mcp` с несколькими скоупами по пути (агрегирующий, на группу, на отдельный tool)
- Динамического формирования наборов tools в runtime через JWT-токены (этап 3)

### 1.3. Целевые потребители
- Dify chatflow/workflow
- n8n workflows
- Python-проекты через официальный MCP SDK
- Любые клиенты, совместимые с MCP-протоколом (Streamable HTTP / SSE)

---

## 2. Термины и определения

| Термин | Определение |
|---|---|
| **Tool** | Единица функциональности MCP-сервера (функция, доступная LLM) |
| **Server** | Подключённый нижестоящий MCP-сервер (Playwright, BrightData и т.п.) |
| **Group** | Именованный набор tools (может включать tools из разных серверов) |
| **Registry** | Реестр всех известных tools с маппингом `tool → server` и `group → tools` |
| **MCP-endpoint** | HTTP-путь, на котором оркестратор ведёт себя как MCP-сервер |
| **Stateless-режим** | Режим работы, при котором конфигурация не меняется в runtime |

---

## 3. Архитектура

### 3.1. Общая схема

```
┌─────────────────────────────────────────────────────────────┐
│  Потребители (Dify / n8n / Python)                          │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP (Streamable HTTP / SSE)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                      mcp_router                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  MCP-endpoint /mcp (единый ScopedMCPServer):           │  │
│  │   /mcp            — агрегатор всех tools               │  │
│  │   /mcp/{group}       — tools группы                    │  │
│  │   /mcp/{server}.{tool} — один tool (синтаксич. сахар)  │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Tool Registry + Groups (центральная логика)          │  │
│  └──────────────────────────┬────────────────────────────┘  │
│  ┌──────────────────────────▼────────────────────────────┐  │
│  │  MCP-Client (агрегатор подключений)                   │  │
│  │  - Connection Pool per server                         │  │
│  │  - Circuit Breaker + exponential backoff              │  │
│  │  - Tools cache (TTL)                                  │  │
│  └──────────────────────────┬────────────────────────────┘  │
│  ┌──────────────────────────▼────────────────────────────┐  │
│  │  REST API + Admin SPA                                 │  │
│  └───────────────────────────────────────────────────────┘  │
└──────────────────────────┬──────────────────────────────────┘
                           │ docker network / HTTP
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   Playwright-MCP    BrightData-MCP    ... другие
```

### 3.2. Принципы

1. **MCP-совместимость**: endpoint `/mcp` ведёт себя как полноценный MCP-сервер (handshake, capabilities, `tools/list`, `tools/call`), отдавая набор tools в зависимости от пути.
2. **Централизация логики**: фильтрация, валидация, логирование — в одном месте (`registry`), не дублируются.
3. **Единый ScopedMCPServer**: один экземпляр `MCPServer` на `/mcp`; `FilterSpec` вычисляется на каждый запрос из пути (агрегатор / группа / отдельный tool / JWT).
4. **Stateless: runtime-состояние не меняется через API.
5. **Graceful degradation**: при недоступности одного сервера остальные продолжают работать.
6. **Безопасность секретов**: в `config.yaml` хранятся только **имена** переменных окружения. Реальные значения резолвятся приложением из `os.environ` при старте.

---

## 4. Функциональные требования по этапам

### Этап 1 — Stateless-агрегатор (MVP)

**Цель**: рабочий агрегатор, совместимый с любым MCP-клиентом.

#### 4.1.1. MCP-endpoint

MCP-сервер монтируется **единственный** на путь `/mcp`. Какой набор tools отдаётся — определяется на **каждый запрос** из пути (`FilterSpec`), единый `ScopedMCPServer` фильтрует по запросу:

| Path (после монтирования на `/mcp`) | Поведение |
|---|---|
| `/mcp` | MCP-сервер со **всеми** tools со всех `enabled` серверов |
| `/mcp/{group}` | MCP-сервер с tools указанной группы (только доступные) |
| `/mcp/{server}.{tool}` | MCP-сервер с единственным tool (синтаксический сахар) |

**Важно**:
- `/mcp/{server}` не реализуется из-за коллизии имён с группами.
- `{server}.{tool}` обрабатывается автоматически для всех известных tools.
- Единый endpoint `/mcp` полностью разводит три интерфейса: MCP (`/mcp`), runtime-REST (`/run/*`) и админку (`/api/*`).
- Имена групп/серверов не должны совпадать с `api` / `run` / `mcp` (проверяется при валидации конфига).

#### 4.1.2. Конфигурация

Статический файл `config.yaml`, загружается при старте:

```yaml
servers:
  - name: playwright
    url: http://playwright:3000/mcp
    headers:
      - X-API-Key: ${PLAYWRIGHT_API_KEY}
    enabled: true
    timeout: 30

  - name: brightdata
    url: https://mcp.brightdata.com/mcp
    headers:
      - Authorization: Bearer ${BRIGHTDATA_TOKEN}
    enabled: true
    timeout: 60

groups:
  - name: web_scraping
    tools:
      - playwright.navigate
      - playwright.click
      - playwright.screenshot
      - brightdata.scrape
    enabled: true

  - name: data_analysis
    tools:
      - playwright.extract_table
    enabled: false
```

**Важно**:
- Секреты передаются через **`${ENV_VAR}`-подстановку** (механизм, аналогичный envsubst): во всём тексте конфига `config.yaml` перед парсингом YAML все вхождения `$\{VAR}` заменяются значениями из `os.environ`. Это покрывает и `url` (например токен в query-параметрах), и значения `headers`.
- Если переменная окружения не найдена — выбрасывается ошибка валидации (fail fast).
- Заголовки аутентификации задаются в секции `headers` как **список пар** `Заголовок: значение` (это позволяет одному серверу передавать ключ разными способами — `Bearer`, `X-API-Key` и т.д.).
- `config.yaml` можно и нужно безопасно коммитить в Git, так как он не содержит реальных секретов.

#### 4.1.3. REST API (read-only)

| Endpoint | Описание |
|---|---|
| `GET /api/health` | Health check (Liveness + Readiness) |
| `GET /api/servers` | Список серверов со статусом (`connected` / `disconnected` / `error`) |
| `GET /api/servers/{name}/list` | Tools конкретного сервера (если доступен) |
| `GET /api/groups` | Список групп с метаданными |
| `GET /api/groups/{name}/list` | Tools группы с указанием сервера и статуса доступности |
| `GET /api/tools` | Все известные tools с указанием сервера, статуса и списка групп |
| `GET /api/tool/{server}.{tool}` | Полная информация по конкретному tool'у (включая JSON-схему вызова `input_schema`); 404, если tool не найден |
| `GET /` | Admin SPA (минимальный UI поверх REST API) |

**`GET /api/tool/{server}.{tool}`** — полная информация по одному tool'у (добавлен при доработке админки; используется карточкой tool'а).

```http
GET /api/tool/mock.navigate
```

Ответ `200`:
```json
{
  "name": "mock.navigate",
  "server": "mock",
  "description": "Перейти по URL",
  "input_schema": {
    "type": "object",
    "properties": { "url": { "type": "string" } },
    "required": ["url"]
  },
  "available": true,
  "groups": ["web"]
}
```

Поля:
- `name` — qualified-имя `server.tool`;
- `server` — имя нижестоящего сервера;
- `description` — описание tool'а;
- `input_schema` — JSON-схема аргументов вызова (используется для построения формы вызова);
- `available` — доступен ли сервер (`connected`);
- `groups` — список групп, в которые входит tool.

Ответ `404`, если tool с таким qualified-именем не найден в реестре.

#### 4.1.4. Реализация единого ScopedMCPServer

Используется **один** экземпляр `ScopedMCPServer` (подкласс `MCPServer` из `mcp==2.3.0`), смонтированный на `/mcp`. `FilterSpec` вычисляется на каждый запрос из пути (`context.request_context.request.url.path`), что автоматически отражает изменения реестра (health-статусы, релоад групп, JWT) без пересоздания серверов.

```python
class ScopedMCPServer(MCPServer):
    def __init__(self, registry, mcp_client, jwt_service=None):
        super().__init__(name="mcp_router")
        self._registry = registry
        self._mcp_client = mcp_client
        self._jwt_service = jwt_service

    def _filter_spec_from_path(self, context) -> FilterSpec:
        path = context.request_context.request.url.path
        rest = path.removeprefix("/mcp").strip("/")
        if not rest:
            return FilterSpec.all()
        if "." in rest:
            return FilterSpec.single(rest)          # {server}.{tool}
        return FilterSpec.group(rest)               # {group}

    async def list_tools(self) -> list[MCPTool]:
        spec = self._filter_spec_from_path(current_context())
        return registry.mcp_tools(spec)

    async def call_tool(self, name, arguments, context=None) -> CallToolResult:
        spec = self._filter_spec_from_path(context)
        if not registry.is_tool_available(name, spec):
            raise ToolError(f"Tool {name} not in scope")
        return await mcp_client.call_tool(name, arguments)

# При старте:
app.mount("/mcp", ScopedMCPServer(registry, mcp_client).streamable_http_app(
    streamable_http_path="/mcp"))
```

**Примечания**:
- Фильтрация, валидация, логирование сосредоточены в `registry` — не дублируются.
- JWT-фильтрация на `/mcp` (этап 3) добавляется в тот же `_filter_spec_from_path`, учитывая заголовок/query-параметр.

---

### Этап 2 — CRUD для групп

> **Отменён** по результатам пробной эксплуатации (см. раздел 4.4). Управление группами остаётся статическим — через `config.yaml` + редеплой контейнера.

**Цель**: возможность управления группами без перезапуска контейнера.

**Важно**: серверы **не управляются** через API — только через `config.yaml` + `.env`.

#### 4.2.1. REST-endpoints для групп

| Endpoint | Описание |
|---|---|
| `POST /api/groups` | Добавить группу |
| `PUT /api/groups/{name}` | Обновить группу |
| `DELETE /api/groups/{name}` | Удалить группу |
| `POST /api/groups/{name}/add` | Добавить tools в группу |
| `POST /api/groups/{name}/remove` | Удалить tools из группы |
| `POST /api/groups/{name}/setup` | Задать tools группы целиком |
| `POST /api/persist` | Сохранить текущее состояние групп в `config.yaml` |
| `POST /api/reload` | Перечитать `config.yaml` с диска |

#### 4.2.2. Атомарная запись конфига

```python
def atomic_save(path: Path, data: dict):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(yaml.dump(data))
    os.replace(tmp, path)  # атомарно на POSIX
```

**Важно**: при `persist` сохраняются **только группы**, секция `servers` остаётся неизменной (читается из исходного `config.yaml`).

#### 4.2.3. Защита REST API (опциональная)

- Если env var `MCP_ROUTER_API_KEY` задан — проверяем header `Authorization: Bearer <key>`
- Если не задан — не проверяем
- MCP-endpoint (`/mcp`, `/mcp/{group}`, `/mcp/{server}.{tool}`) не требует аутентификации (доступ только из docker-сети)

---

### Этап 3 — Динамические группы через JWT (runtime)

**Цель**: адресная передача набора tools от узла workflow к LLM-агенту с сохранением stateless.

#### 4.3.1. Endpoint `/run/select`

```http
POST /run/select
Content-Type: application/json
Authorization: Bearer <api_key>  # если задан

{
  "tools": ["playwright.click"],
  "ttl": 3600
}
```

Ответ:
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_at": "2026-10-19T15:30:00Z",
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "playwright_click",
        "description": "...",
        "parameters": {...}
      }
    }
  ]
}
```

**JWT payload:**
```json
{
  "tools": ["playwright.click"],
  "exp": 1729345800
}
```

**Ключевая фича**: поле `tools` возвращается в формате OpenAI function calling — это позволяет использовать tools напрямую в LLM API **без MCP-клиента**.

**Семантика `tools` в `/run/select`:**
- **Отсутствует или пустой список** → режим **агрегатора**: JWT получает маркер
  `["*"]` (константа `ALL_TOOLS`), а в ответе `tools` — **все** доступные tools
  реестра. На `/mcp` такой токен отдаёт все tools.
- **Непустой список** → валидация:
  - tools, **не найденные в реестре** → `400 Bad Request` с `{"detail": "Tools не найдены: [...]"}`;
  - tools, чей MCP-сервер **недоступен** (выключен/ошибка) → `400 Bad Request`
    с `{"detail": "Tools недоступны: [...]"}`;
  - иначе — JWT на указанное подмножество.

#### 4.3.1a. Endpoint `/run/select/{group}`

Выбор tools, ограниченный составом заданной группы (вместо всего реестра).

```http
POST /run/select/{group}
Content-Type: application/json
Authorization: Bearer <api_key>  # если задан

{
  "tools": ["playwright.click"],
  "ttl": 3600
}
```

**Семантика:**
- **Группа не найдена** → `404 Not Found`.
- **`tools` отсутствует или пустой список** → JWT на **всю группу** (tools,
  входящие в группу), в ответе `tools` — все tools группы. (В отличие от
  `/run/select`, здесь пустой список **не** означает «все tools» глобально —
  только состав группы.)
- **Непустой список** → валидация **по составу группы**:
  - tools, **не входящие в группу** → `400 Bad Request`
    с `{"detail": "Tools не найдены: [...]"}` (проверяется только наличие в группе,
    без разметки причин);
  - tools, чей MCP-сервер **недоступен** → `400 Bad Request`
    с `{"detail": "Tools недоступны: [...]"}`;
  - иначе — JWT на указанное подмножество группы.

Ответ по структуре идентичен `/run/select` (`{token, expires_at, tools}`).

**JWT-семантика пустого списка** (общая для обоих эндпоинтов): маркер `["*"]`
означает «все tools» (агрегатор) и интерпретируется на `/mcp` как `FilterSpec.all()`.
Невалидный/просроченный JWT по-прежнему даёт **пустой** скоуп (безопасно не отдаёт
ничего), а не режим агрегатора.

#### 4.3.2. Использование JWT в MCP-endpoint `/mcp`

При обращении к `/mcp`:
1. Извлечь JWT из header `Authorization: Bearer <jwt>` **или** query-параметра `?token=<jwt>`
2. Распарсить, проверить подпись и `exp`
3. При `tools/list` — вернуть из registry только те tools, что в JWT (отфильтровать)
4. При `tools/call` — проверить, что tool есть в JWT, проксировать на соответствующий сервер

**Если JWT не передан** — возвращаем все tools (режим агрегатора, обратная совместимость).

#### 4.3.3. Stateless-гарантии

- Нет хранения UUID/JWT на стороне сервера
- TTL встроено в claim `exp`
- Масштабирование: любой инстанс mcp_router обработает запрос
- Подпись HMAC защищает от подделки

#### 4.3.4. REST-эндпоинт для прямого вызова инструмента

Для упрощения интеграции с no-code/low-code платформами (n8n, Dify через HTTP Request) и кастомными скриптами, добавляется прямой REST-вызов инструмента без необходимости реализовывать полноценный JSON-RPC клиент MCP.

**Эндпоинт:**
```http
POST /run/tool/call
Content-Type: application/json

{
  "name": "playwright.click",
  "arguments": {
    "selector": "#submit-button",
    "timeout": 5000
  }
}
```

**Ответ в виде строки (json.dumps):**
```json
{
  "status": "success",
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Button clicked successfully"
      }
    ],
    "isError": false
  }
}
```
*(Формат ответа эквивалентен стандартному ответу MCP `tools/call`, сериализованному в строку).*

**Важно**: Проверка JWT-токена на этом эндпоинте **не требуется**. Это сделано намеренно, чтобы избежать лишней точки отказа и усложнения интеграции для простых сценариев. Безопасность обеспечивается тем, что эндпоинт доступен только внутри доверенной docker-сети (или через опциональный `MCP_ROUTER_API_KEY`, если он задан глобально для REST API).

---

## 4.4. Уточнение технических решений по результатам пробной эксплуатации

По итогам пробных запусков контейнера внесены изменения в проект (задним числом корректируют изначальные требования).

### 4.4.1. Структура конфигурации и передача секретов

Изначальная схема `auth` оказалась неудачной:

- ключи авторизации могут передаваться внешним MCP-серверам **разными способами** (`Authorization: Bearer ...`, `X-API-Key: ...`, токен в query-параметре `url` и т.д.), и это разнообразие не поддерживалось;
- иерархия полей YAML (`servers[].auth.headers`) была неестественной и неочевидной.

**Принятые решения** (см. §4.1.2):

1. Введена **`${ENV_VAR}`-подстановка по всему тексту конфига** (аналог envsubst). Перед парсингом YAML все вхождения `$\{VAR}` заменяются значениями из окружения; при отсутствии переменной — fail fast. Это покрывает и `url`, и значения `headers` единообразно, вместо старого механизма «имя переменной как обычная строка».
2. Секция `auth` заменена на **`headers`** — список пар `Заголовок: значение`:
   ```yaml
   servers:
     - name: brightdata
       url: https://mcp.brightdata.com/mcp
       headers:
         - Authorization: Bearer ${BRIGHTDATA_TOKEN}
   ```
   Такой формат естественно выражает «набор HTTP-заголовков, которыми серверу передаётся ключ», и не привязывается к конкретному способу аутентификации.

### 4.4.2. Отмена Этапа 2 (CRUD для групп)

По результатам пробной эксплуатации **Этап 2 отменён**. Обоснование:

- модель развёртывания («один контейнер на сервер + оркестратор») и так предполагает редеплой для смены конфигурации — изменение групп через редеплой не создаёт дополнительной сложности;
- группы — статическая, редко меняющаяся сущность, задаваемая один раз при проектировании набора tools;
- полный CRUD потребовал бы синхронизации записи конфига с живым `ClientPool`/`Registry`, частичной записи и атомарного сохранения — это добавило бы больше поверхностей для багов, чем реальной ценности.

**Итог**: управление группами остаётся статическим — через `config.yaml` + редеплой. Этап 3 (JWT-группы) реализуется как было запланировано.

---

## 5. Нефункциональные требования

### 5.1. Надёжность

- **Circuit Breaker** на каждое подключение к MCP-серверу (порог: 5 ошибок за 60 секунд, восстановление через 30 секунд).
- **Exponential backoff** при реконнекте (1s, 2s, 4s, 8s, max 60s).
- **Health check** в фоне каждые 30 секунд — пинг каждого сервера.
- При недоступности сервера его tools помечаются `available: false`, но не удаляются из registry.

### 5.2. Производительность

- **Кэш tools** (`tools/list`) с TTL 5 минут на каждый сервер.
- Ручное обновление кэша через `POST /api/servers/{name}/refresh` (этап 2).
- Таймауты на вызовы tool: настраиваются per-server (дефолт 30s).

### 5.3. Наблюдаемость

- **Структурированные JSON-логи** через `structlog`:
  - `tool.call` (имя, сервер, длительность, статус)
  - `server.health` (статус, латентность)
  - `group.select` (когда вызывается `/run/select`)
- **Метрики Prometheus** (`/metrics`, без аутентификации; через `prometheus-client`):
  - `mcp_router_tool_calls_total{server, tool, status}` — счётчик вызовов tools (ok/error);
    снимается в точке получения ответа `tools/call` от нижестоящего MCP-сервера,
    поэтому покрывает и `/run/tool/call`, и MCP `tools/call` на `/mcp`;
  - `mcp_router_tool_calls_duration_seconds{server, tool}` — гистограмма длительности вызовов;
  - `mcp_router_server_up{server}`, `mcp_router_server_status{server}` (1/2/3),
    `mcp_router_server_tools_total{server}` — gauge статусов серверов;
  - `mcp_router_http_requests_total{method, path, status}` + `mcp_router_http_request_duration_seconds`
    — стандартные HTTP-метрики; динамические пути нормализуются до шаблона (например
    `/run/select/{group}`, `/api/tool/{tool}`, все `/mcp*` → `/mcp`), чтобы не раздувать cardinality;
  - стандартные `process_*`/`python_gc_*`.

### 5.4. Graceful shutdown

- При SIGTERM — закрыть все MCP-сессии с нижестоящими серверами.
- Дождаться завершения активных вызовов tools (таймаут 10s).
- Отдать 503 на новых запросах во время shutdown.

### 5.5. Безопасность

- Все секреты резолвятся из переменных окружения по именам, указанным в `config.yaml`.
- REST API защищён API-ключом (опционально, через env var).
- MCP-endpoints доступны только из docker-сети (binding на `0.0.0.0` внутри контейнера, но без публикации наружу).
- JWT-токены подписаны HMAC, клиент не может изменить список tools.

---

## 6. Развёртывание

### 6.1. Docker-образ

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY . .
CMD ["uvicorn", "mcp_router.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 6.2. docker-compose.yml

```yaml
services:
  mcp_router:
    build: .
    ports:
      - "127.0.0.1:8000:8000"  # только localhost
    environment:
      - MCP_ROUTER_API_KEY=${MCP_ROUTER_API_KEY}  # опционально
      - PLAYWRIGHT_API_KEY=${PLAYWRIGHT_API_KEY}
      - BRIGHTDATA_TOKEN=${BRIGHTDATA_TOKEN}
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
    networks:
      - mcp_net

networks:
  mcp_net:
    driver: bridge
```

---

## 7. Тестирование

### 7.1. Уровни тестов

| Уровень | Инструмент | Покрытие |
|---|---|---|
| Unit | pytest | config (включая резолвинг env vars), registry, фабрика MCPServer, JWT |
| API | FastAPI TestClient | все REST-endpoints |
| Contract | Mock MCP-сервер | handshake, ошибки, таймауты, недоступность |
| Integration | docker-compose + реальный Playwright | end-to-end |

### 7.2. Mock MCP-сервер для контрактных тестов

```python
class MockMCPServer:
    """Эмулирует MCP-сервер с настраиваемым поведением"""
    def __init__(self, delay: float = 0, fail_tools: bool = False):
        ...
```

Сценарии:
- Медленный ответ (> timeout)
- Падение во время handshake
- Пустой список tools
- Невалидный JSON в ответе
- Разрыв SSE-соединения

### 7.3. Покрытие для MCP-endpoints

- Handshake на `/mcp`, `/mcp/{group}`, `/mcp/{server}.{tool}`
- `tools/list` возвращает корректный набор для каждого path
- `tools/call` проксируется на правильный сервер
- Обработка недоступного сервера (fallback)
- Фильтрация tools через JWT на `/mcp`

---

## 8. Риски и митигации

| Риск | Вероятность | Влияние | Митигация |
|---|---|---|---|
| Нестабильность MCP Python SDK (streamable HTTP) | Средняя | Высокое | Зафиксировать версию `mcp==2.3.0`; абстракция транспорта |
| Отсутствие env-переменной при старте | Низкая | Критическое | Pydantic-валидация: падение с понятной ошибкой, если имя из конфига не найдено в `os.environ` |
| Утечка сессий при реконнектах | Средняя | Средняя | Circuit Breaker + TTL на сессии |
| Race condition при persist | Низкая | Средняя | Атомарная запись через `os.replace` |
| Раздувание контекста при `/mcp` | Высокое | Низкое | Документация: использовать `/mcp/{group}` или JWT для LLM |
| Несовместимость с конкретным MCP-клиентом | Средняя | Средняя | Тестирование с Dify, n8n, Python SDK |

---

## 9. Критерии приёмки

### Этап 1 (MVP)
- [ ] Подключается к Playwright MCP-серверу в docker-сети
- [ ] Отдаёт все tools на `/mcp`
- [ ] Отдаёт tools группы на `/mcp/{group}`
- [ ] Отдаёт один tool на `/mcp/{server}.{tool}`
- [ ] Совместим с Dify как MCP-сервер
- [ ] REST API возвращает списки серверов/групп/tools
- [ ] Health check работает
- [ ] Graceful shutdown закрывает сессии
- [ ] Конфиг корректно выполняет `$\{VAR}`-подстановку из окружения в значения

### Этап 2
- [x] **Отменён** по результатам пробной эксплуатации (см. §4.4.2). Управление группами — статическое, через `config.yaml`.

### Этап 3
- [ ] `/run/select` возвращает JWT + tools в формате OpenAI
- [ ] `/run/select` без payload или с пустым `tools` — режим агрегатора (все tools)
- [ ] `/run/select` с `tools` вне реестра — 400 с перечнем «Tools не найдены»
- [ ] `/run/select` с недоступными серверами tools — 400 с «Tools недоступны»
- [ ] `/run/select/{group}` — выбор tools по составу группы
- [ ] `/run/select/{group}` без payload — вся группа целиком
- [ ] `/run/select/{group}` с несуществующей группой — 404
- [ ] `/run/select/{group}` с `tools` вне группы — 400 с перечнем
- [ ] MCP-endpoint `/mcp` фильтрует tools через JWT (header или query)
- [ ] Если JWT не передан — режим агрегатора (все tools)
- [ ] Пустой/`["*"]` JWT — режим агрегатора; невалидный/просроченный — пустой скоуп
- [ ] TTL встроено в JWT claim `exp`
- [ ] Эндпоинт `/run/tool/call` корректно проксирует вызов инструмента и возвращает результат в строковом формате, совместимом с ожиданиями LLM при `tool_calls`
- [ ] Prometheus-метрики на `/metrics`: `mcp_router_tool_calls_total{server,tool,status}` и гистограмма длительности, покрывающие и `/run/tool/call`, и MCP `tools/call` на `/mcp`
- [ ] Gauge статусов серверов: `mcp_router_server_up`, `mcp_router_server_status`, `mcp_router_server_tools_total`
- [ ] HTTP-метрики с нормализацией динамических путей

---

## 10. Зафиксированные решения

| Вопрос | Решение |
|---|---|
| Версия MCP SDK | `mcp==2.3.0` (или `mcp[cli]==2.3.0`) |
| MCP-endpoint | Единый `ScopedMCPServer` (single instance) на `/mcp`, фильтрация по path на каждый запрос |
| Формат конфига | YAML |
| Админка | HTML + JS (без фреймворка) |
| Логирование | `structlog` |
| Динамические группы | JWT (не UUID) — полный stateless |
| JWT-секрет | Из env-переменной `MCP_ROUTER_JWT_SECRET` (HMAC-подпись) |
| Управление серверами | Только через `config.yaml` + `.env` (не через API) |
| Управление группами | Статическое, через `config.yaml` + редеплой (Этап 2 отменён, см. §4.4.2) |
| Защита REST API | Опциональная, через env var `MCP_ROUTER_API_KEY` |
| Хранение секретов | `$\{VAR}`-подстановка по всему тексту `config.yaml` из окружения (fail fast); заголовки — в секции `headers` как список пар `Заголовок: значение` (см. §4.4.1) |

---


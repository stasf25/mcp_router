# Тестовый набор mcp_router

Документ описывает набор тестов проекта `mcp_router`, их назначение, структуру
и инструкцию по запуску.

---

## 1. Быстрый запуск

```bash
source venv            # активация окружения (создаётся через `source make_venv`)
python -m pytest -q    # полный прогон тестов
ruff check src tests   # линтер
```

Ожидаемый результат: **135 passed**, ruff — без замечаний.

### Полезные варианты запуска

```bash
python -m pytest -q                      # краткий вывод
python -m pytest                          # подробный вывод
python -m pytest tests/test_config.py     # только модуль
python -m pytest -k "jwt"                 # только тесты с "jwt" в имени
python -m pytest -q -x                    # остановиться на первом падении
python -m pytest -q --disable-warnings    # без предупреждений
```

---

## 2. Структура набора

Тесты лежат в `tests/`. Вспомогательные модули:

| Файл | Назначение |
|---|---|
| `conftest.py` | Общие фикстуры: `mock_server_app`, `running_mock_server`, `router_url`, `_wait_http_ready`, фикстуры сессий (`router_url_with_session`, `router_url_with_session_default_off`) |
| `mock_mcp.py` | Mock нижестоящий MCP-сервер, запуск Starlette/uvicorn в фоновом потоке, свободный порт, mock с захватом `_meta` (`build_meta_capture_mock_server`) |

Тестовые модули:

| Файл | Что покрывает |
|---|---|
| `test_config.py` | Загрузка/валидация `config.yaml`: базовый конфиг, `${ENV_VAR}`-подстановка в `headers` и `url`, fail fast на отсутствующей переменной, статичный заголовок, дубли имён серверов, невалидный URL, отсутствующий файл, атомарное сохранение, сериализация, `circuit_breaker` (дефолты, кастомные значения, валидация) |
| `test_registry.py` | Единичные (unit) тесты `Registry`/`FilterSpec`: фильтрация `all`/`group`/`single`/`jwt`, доступность tools |
| `test_mcp_client.py` | Unit-тесты `CircuitBreaker` (открытие по порогу, сброс по успеху, half-open после recovery) и передача параметров breaker из конфига в `DownstreamClient`; unit-тесты версии протокола: `protocol_version`/`is_v2` (True/False/без клиента), сбор исходящего `_meta` (`_build_outgoing_meta`): v2→v2 проброс пользовательских ключей, v2→v1 (без `_meta`), приоритет `session_id`/`session_id_key` над пользовательскими ключами; unit-тесты `_extract_client_context`: фильтрация зарезервированных ключей `io.modelcontextprotocol/*`, v1-сессия из `_meta`/заголовка `mcp-session-id` |
| `test_api.py` | REST-эндпоинты админки `/api/*`: health, servers, groups, tools, tool detail, 404; DNS-rebinding защита SDK — разрешённые Host-заголовки (`mcp_router`, `localhost`, `127.0.0.1`) проходят host-check, посторонний Host отклоняется 421 |
| `test_contract.py` | Контрактные тесты MCP-протокола через внешний `Client` (`mcp` SDK): handshake, `tools/list` на `/mcp`, `/mcp/{group}`, `/mcp/{server}.{tool}`, `tools/call`, ошибка для tool вне скоупа, эндпоинт tool detail, админка. Проброс контекста: v1→v2 проброс `session_id` в `_meta` (`test_session_id_passthrough_to_downstream`, legacy-клиент), v2→v2 проброс пользовательских ключей `_meta` с фильтрацией зарезервированных (`test_v2_meta_passthrough_to_downstream`), проверка, что проброс `session_id` выключен по умолчанию (`session_id_key=None`) |
| `test_run_api.py` | Этап 3: JWT (`jwt_service`), `/run/select` (пустой список → все tools, 400 на отсутствующие/недоступные, `expires_at` вычисляется из `ttl`), `/run/select/{group}` (404 на группу, пустой → вся группа, 400 на tools вне группы), `/run/tool/call` (успех; неизвестный tool → всегда 200 с `isError: true` в результате), JWT-фильтрация на `/mcp` через header и query (для `list_tools` и `call_tool`), невалидный JWT, защита `/run/*` API-ключом (отсутствует/неверный → 401, верный → 200) |
| `test_metrics.py` | Prometheus-метрики: `/metrics` отдаёт exposition, `mcp_router_tool_calls_total` пишется и через `/run/tool/call`, и через MCP `tools/call` на `/mcp`, gauge статусов серверов, HTTP-метрика с нормализацией пути, unit-тесты `normalize_path` |
| `test_graceful_degradation.py` | Интеграционный тест graceful degradation (ТЗ §5.1): flaky-сервер через `FailSwitch`/`build_flaky_mock_server`; рабочий вызов → `isError: false`; падение сервера → 200 с `isError: true`; после срабатывания circuit breaker (порог 2) роутер не падает, продолжает отвечать 200 с `isError: true` |
| `test_protocol_gate.py` | Гейт версии протокола `MCP_PROTOCOL_VERSION` (auto/v1/v2): при `v1` v1-клиент проходит, v2-клиент получает `406`; при `v2` наоборот; `auto` принимает обе эры; fail-fast на невалидном значении (`protocol_version_mode`) |
| `test_logging.py` | Логирование подключений с версией протокола: `server.connected` (downstream, поля `protocol_version`/`protocol_era`), `mcp.client_connected` (внешние клиенты на `/mcp`: legacy → `version=v1`, modern → `version=v2`) через `structlog.testing.capture_logs`; проверка, что логирование не влияет на фильтр `MCP_PROTOCOL_VERSION` (при `v1` v2-клиент отклонён и не логируется, v1-клиент проходит и логируется) |

---

## 3. Fixtures и mock MCP-сервер

### 3.1. `mock_mcp.py`

- `build_mock_server(name)` — строит `MCPServer` (mcp 2.x) с тремя tools:
  `navigate`, `click`, `screenshot` (имена без префикса сервера);
- `build_meta_capture_mock_server(name)` — mock с tool `echo_meta(context)` и
  `session_id_key`, который **возвращает** полученный `_meta` в результате вызова —
  используется для проверки проброса контекста v1/v2;
- `build_flaky_mock_server(name, switch)` + `FailSwitch` — mock с tool `ping`,
  который можно заставить падать в runtime через `switch.fail` (для
  интеграционного теста graceful degradation / circuit breaker);
- `serve_mcp_server_on_thread(app, port, path="/")` — запускает uvicorn в фоновом
  потоке (`should_exit=True` — аналог graceful shutdown без Docker, которого нет
  в среде разработки);
- `free_port()` — подбирает свободный TCP-порт.

### 3.2. `conftest.py` — фикстуры

| Фикстура | Назначение |
|---|---|
| `mock_server_app` | Starlette-app mock-сервера |
| `running_mock_server` | Запускает mock в фоновом потоке, возвращает URL `http://127.0.0.1:<port>/mcp` |
| `router_url` | Строит `config.yaml` (сервер `mock` + группы `web`, `screens`), создаёт роутер через `create_app()`, запускает его в фоновом потоке, ждёт готовности `/api/health`, возвращает base URL `http://127.0.0.1:<port>` |
| `router_url_with_session` | Роутер с `session_id_key` у сервера (`mock`), проброс `session_id` включён; mock-сервер захватывает `_meta` (для v1→v2 session-тестов) |
| `router_url_with_session_default_off` | Роутер с сервером **без** `session_id_key` (по умолчанию `None`) — проверка, что проброс контекста выключен (обратная совместимость) |

`router_url` — ключевая фикстура: поднимает **реальное** приложение `mcp_router`
(uvicorn) вместе с реальным mock нижестоящим сервером. Контрактные и run-тесты
работают поверх живого HTTP.

---

## 4. Уровни тестов

1. **Unit** (`test_registry.py`, `test_config.py`, `test_mcp_client.py`, часть `test_run_api.py`):
   без сети, быстрые.
2. **API-тесты** (`test_api.py`): REST-эндпоинты роутера, работают через
   `router_url` (живой роутер + mock).
3. **Контрактные** (`test_contract.py`): полный MCP-протокол через внешний
   `Client` из `mcp` SDK — handshake, tools/list, tools/call на реальных портах.
4. **Run-тесты этапа 3** (`test_run_api.py`): JWT + `/run/*` + фильтрация на `/mcp`.
5. **Метрики** (`test_metrics.py`): `/metrics`, счётчики вызовов tools, gauge
   статусов, нормализация HTTP-путей (через `router_url`).
6. **Гейт версии протокола** (`test_protocol_gate.py`): `MCP_PROTOCOL_VERSION`
   (auto/v1/v2) — интеграционные проверки `406`-отклонений по эре клиента
   (через `create_app` с установленным env) + unit-тесты `protocol_version_mode`.
7. **Логирование подключений** (`test_logging.py`): перехват `structlog`
   (`capture_logs`) — `server.connected` для downstream и `mcp.client_connected`
   для внешних клиентов `/mcp` (v1/v2).

---

## 5. Docker / окружение

Docker-демон в среде разработки недоступен (`docker ps` → no daemon), поэтому
контрактные тесты поднимают приложение через uvicorn в фоновом потоке
(`should_exit=True`), что эквивалентно graceful shutdown. Для полной проверки
контейнерного запуска используется ручной запуск через uvicorn:

```bash
uvicorn mcp_router.main:create_app --factory --host 0.0.0.0 --port 8000
```

---

## 6. Предупреждения (warnings)

При запуске могут появляться предупреждения `InsecureKeyLengthWarning` от PyJWT
в unit-тестах `jwt_service` (если использовать короткий секрет). В реальном коде
секрет берётся из `MCP_ROUTER_JWT_SECRET` (рекомендуемая длина ≥ 32 байт) или
генерируется случайно — поэтому в проде предупреждение отсутствует.

"""Web-интерфейс админки.

HTML+JS лежат отдельно от кода — в ``static/admin.html`` (пакетный ресурс).
Модуль отдаёт этот файл по ``GET /`` и раздаёт статику.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter, Response

router = APIRouter(include_in_schema=False)

_ADMIN_HTML = (files("mcp_router") / "static" / "admin.html").read_text(encoding="utf-8")


@router.get("/")
async def admin_index() -> Response:
    return Response(_ADMIN_HTML, media_type="text/html; charset=utf-8")

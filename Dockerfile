FROM python:3.10-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml .
COPY src ./src
RUN pip install --no-cache-dir .

COPY config.yaml .

EXPOSE 8000

CMD ["uvicorn", "mcp_router.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

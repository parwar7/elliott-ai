FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ELLIOTT_PROVIDER=openai \
    ELLIOTT_WORKSPACE=/app \
    ELLIOTT_DATABASE=/data/elliott_ai.sqlite3 \
    ELLIOTT_SEED_DATABASE=/app/seed/elliott_ai.sqlite3 \
    ELLIOTT_CLOUD_DATA_ROOT=/data

WORKDIR /app

RUN mkdir -p /app/seed /data

COPY elliott_ai/ /app/elliott_ai/
COPY AI_BRAIN_MASTER_RULES.json AI_BRAIN_CURRENT.md /app/
COPY .elliott_ai/elliott_ai.sqlite3 /app/seed/elliott_ai.sqlite3

CMD ["python", "-m", "elliott_ai.telegram_bot"]

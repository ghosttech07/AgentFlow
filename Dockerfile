# AgentFlow app image (used by `docker compose --profile full up -d --build`).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home agentflow && chown -R agentflow /app
USER agentflow

EXPOSE 8000
# Migrate, create the read-only sample database (idempotent), then serve.
CMD ["sh", "-c", "alembic upgrade head && python scripts/setup_sample_data.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

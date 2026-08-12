FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv

COPY pyproject.toml ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN pip install --no-cache-dir .

EXPOSE 8080

# Cloud Run injects PORT; shell form so the env var expands, exec so uvicorn gets signals.
CMD exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT}

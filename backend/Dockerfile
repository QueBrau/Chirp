FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv

# Dependencies come from requirements.lock, NOT from pyproject.toml's open ranges.
#
# Before this, both this image and CI ran `pip install ".[firebase]"` against
# constraints that were open-ended (fastapi, uvicorn, asyncpg, alembic, httpx had
# no bound at all; sqlalchemy, pydantic, redis and stripe had a floor only). Two
# deploys of byte-identical source could therefore install different versions of
# stripe or firebase-admin — the packages that take payments and verify every auth
# token — with no PR, no review and no signal anywhere that anything had changed.
# CI installed the same open way, so a green check did not describe what shipped.
#
# The lock is generated for linux/py3.12 specifically because that is what this
# image and the CI runner both are; regenerate it with the command recorded at the
# top of the file. Copied before the app source so this layer stays cached until
# the dependency set itself actually changes.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# --no-deps is load-bearing: every dependency is already installed above at a
# pinned version. Without it pip re-resolves from pyproject's open ranges and can
# quietly upgrade one of them, which would defeat the entire point of the lock.
RUN pip install --no-cache-dir --no-deps .

# Root has no job left to do once the packages and source are in place: the app
# never writes to /srv at runtime (config comes from env vars and Secret Manager,
# not files), so this drops privileges before a single line of app code runs. Cuts
# blast radius if a dependency (or the app itself) is ever exploited — root inside
# the container would otherwise be one less thing standing between an RCE and the
# host's container runtime.
RUN useradd --system --no-create-home --uid 1000 appuser
USER appuser

EXPOSE 8080

# Cloud Run injects PORT; shell form so the env var expands, exec so uvicorn gets signals.
CMD exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT}

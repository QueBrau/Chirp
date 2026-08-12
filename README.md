# Chirp

Mobile-first social + operations platform for fraternity/sorority chapters: E2EE messaging,
chapter feed, anonymous campus board, big/little lineage trees, dues collection, and role-based
dashboards. Monorepo: `backend/` (FastAPI) + `app-mobile/` (Expo). See `SPEC.md` and
`CONVENTIONS.md` before contributing.

## Quickstart

### 1. Infrastructure (Postgres 16 + Redis 7)

```bash
docker compose up -d
```

### 2. Backend (FastAPI)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:create_app --factory --reload
```

API is now at `http://127.0.0.1:8000` (health check: `GET /healthz`).

### 3. Mobile app (Expo)

```bash
cd app-mobile
npm install
npx expo start
```

> **Dev build required.** libsignal needs native modules, so the app does NOT run in Expo Go.
> Use `npx expo prebuild` + `npx expo run:ios` / `run:android`, or an EAS development build.
> `npx expo start` still works for iterating on screens against a dev build client.

## Emulated auth (local dev)

The backend defaults to `AUTH_MODE=emulated`: instead of verifying a Firebase JWT, it trusts the
`X-Debug-Firebase-Uid` header as the caller's Firebase uid. Example:

```bash
curl -H "X-Debug-Firebase-Uid: dev-user-1" http://127.0.0.1:8000/conversations
```

`POST /auth/bootstrap` is the only route that accepts an authenticated-but-unregistered identity
(it creates the `users` row). Set `AUTH_MODE=firebase` (plus `FIREBASE_PROJECT_ID` and the
`firebase` extra: `pip install -e ".[firebase]"`) for real token verification.

## Configuration

Settings load from env / `backend/.env` (see `app/config.py`): `DATABASE_URL`, `REDIS_URL`,
`AUTH_MODE`, `FIREBASE_PROJECT_ID`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `CORS_ORIGINS`.
Defaults point at the docker-compose services.

## Tests

```bash
cd backend
pytest
```

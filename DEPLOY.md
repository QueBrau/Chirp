# Chirp — GCP Deploy Guide (backend)

Deploys `backend/` (FastAPI) to **Cloud Run**, with **Cloud SQL** (Postgres 16),
**Memorystore** (Redis), **Secret Manager**, and **Firebase Auth**. The Dockerfile
is already Cloud Run-ready (listens on `$PORT`, uvicorn factory). Pairs with
`SETUP-FIREBASE.md` (auth) and the go-live board cards.

Set once:
```bash
export PROJECT=chirp-prod          # your GCP project id
export REGION=us-central1
gcloud config set project $PROJECT
```

## 0. Enable APIs
```bash
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  redis.googleapis.com secretmanager.googleapis.com vpcaccess.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com identitytoolkit.googleapis.com
```

## 1. Cloud SQL — Postgres 16
```bash
gcloud sql instances create chirp-db --database-version=POSTGRES_16 \
  --tier=db-f1-micro --region=$REGION
gcloud sql databases create chirp --instance=chirp-db
gcloud sql users create chirp --instance=chirp-db --password='STRONG_PASSWORD'
```
Note the **connection name** (`$PROJECT:$REGION:chirp-db`) — you need it below.

> **GOTCHA #1 — the DATABASE_URL format.** On Cloud Run, Cloud SQL is reached over a
> unix socket, not host:port. With asyncpg the URL is:
> ```
> postgresql+asyncpg://chirp:STRONG_PASSWORD@/chirp?host=/cloudsql/PROJECT:REGION:chirp-db
> ```
> (empty host before `/chirp`, socket dir in the `host=` query param). A normal
> `host:5432` URL will fail on Cloud Run.

## 2. Memorystore — Redis (needs a VPC connector)
```bash
gcloud redis instances create chirp-redis --size=1 --region=$REGION --redis-version=redis_7_0
gcloud redis instances describe chirp-redis --region=$REGION --format='value(host)'   # INTERNAL_IP
# Cloud Run reaches private Redis only through a Serverless VPC connector:
gcloud compute networks vpc-access connectors create chirp-vpc \
  --region=$REGION --range=10.8.0.0/28
```
`REDIS_URL = redis://INTERNAL_IP:6379/0`. Redis is fan-out only (never storage) —
the app already degrades gracefully if Redis is down, so this can come second.

## 3. Firebase Auth
Do `SETUP-FIREBASE.md` first (Email + Google + Apple). Use the SAME GCP project so
the backend can verify tokens via Application Default Credentials (the Cloud Run
service account) — no service-account key file needed. You only need
`FIREBASE_PROJECT_ID` set on the backend.

## 4. Secrets → Secret Manager
Put anything with a credential in Secret Manager, not plain env:
```bash
printf 'postgresql+asyncpg://chirp:STRONG_PASSWORD@/chirp?host=/cloudsql/%s:%s:chirp-db' "$PROJECT" "$REGION" \
  | gcloud secrets create DATABASE_URL --data-file=-
printf 'redis://INTERNAL_IP:6379/0' | gcloud secrets create REDIS_URL --data-file=-
# stripe keys later (milestone 8)
```

## 5. Run migrations against Cloud SQL
The container runs only uvicorn — migrations are a separate step. Easiest: from your
machine via the Cloud SQL Auth Proxy.
```bash
./cloud-sql-proxy $PROJECT:$REGION:chirp-db &        # listens on 127.0.0.1:5432
cd backend
DATABASE_URL='postgresql+asyncpg://chirp:STRONG_PASSWORD@localhost:5432/chirp' \
  .venv/bin/alembic upgrade head                      # applies 0001..0004
```
Re-run this whenever a new migration lands.

## 6. Deploy to Cloud Run
```bash
cd backend
gcloud run deploy chirp-api --source . --region=$REGION --allow-unauthenticated \
  --add-cloudsql-instances=$PROJECT:$REGION:chirp-db \
  --vpc-connector=chirp-vpc \
  --set-secrets=DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest \
  --set-env-vars=ENV=production,AUTH_MODE=firebase,FIREBASE_PROJECT_ID=$PROJECT,CORS_ORIGINS='["https://YOUR_APP_ORIGIN"]'
```

> **GOTCHA #2 — the production safety guard (SECURITY-REVIEW finding 5).** When
> `ENV` is not `local`, the app REFUSES to start unless `AUTH_MODE=firebase` AND
> `CORS_ORIGINS` has no `"*"`. This is intentional — it stops the emulated-auth
> debug bypass from ever reaching a public URL. So: **set up Firebase (step 3)
> BEFORE deploying with `ENV=production`.** `CORS_ORIGINS` must be JSON-array text.
> If you want a quick private test before Firebase is ready, deploy with
> `ENV=staging` is NOT enough (same guard) — either finish Firebase first, or test
> locally behind ngrok with `ENV=local` (never expose ENV=local publicly).

## Env var reference (Settings → env)
| env var | required | example |
|---|---|---|
| `DATABASE_URL` | yes | asyncpg unix-socket URL (gotcha #1) |
| `REDIS_URL` | for realtime | `redis://10.x.x.x:6379/0` |
| `ENV` | prod | `production` (forces firebase + real CORS) |
| `AUTH_MODE` | prod | `firebase` |
| `FIREBASE_PROJECT_ID` | prod | `chirp-prod` |
| `CORS_ORIGINS` | prod | `["https://app.chirp..."]` (JSON array) |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | milestone 8 | — |

## Fast path for go-live testing (before full GCP)
The board's "shared backend" blocker doesn't strictly need Cloud Run day one — you
can run the backend locally and expose it with `ngrok http 8000`, point the app's
api base URL at the ngrok URL, and both devs integrate against it. Move to Cloud Run
once auth + feed are wired. (Keep `ENV=local` only for private/ngrok, never a public deploy.)

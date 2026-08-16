# Chirp — GCP Deploy Guide (backend)

Deploys `backend/` (FastAPI) to **Cloud Run**, with **Cloud SQL** (Postgres 16),
**Memorystore** (Redis), **Secret Manager**, and **Firebase Auth**. The Dockerfile
is already Cloud Run-ready (listens on `$PORT`, uvicorn factory). Pairs with
`SETUP-FIREBASE.md` (auth) and the go-live board cards.

Set once (`PROJECT` is your GCP project id):
```bash
export PROJECT=chirp-prod
export REGION=us-central1
gcloud config set project $PROJECT
```

> **Every command in this file is meant to be pasted verbatim, so none of them
> carry an inline `#` comment.** Jose's zsh has `interactive_comments` off: a `#`
> line runs as a command, and a trailing `# ...` is passed to the program as an
> argument. Keep it that way when you edit this doc — put the explanation in
> prose above the block.

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
The `describe` prints the internal IP you need below, and Cloud Run reaches
private Redis only through a Serverless VPC connector:
```bash
gcloud redis instances create chirp-redis --size=1 --region=$REGION --redis-version=redis_7_0
gcloud redis instances describe chirp-redis --region=$REGION --format='value(host)'
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
```
Stripe keys come later (milestone 8).

## 5. Run migrations against Cloud SQL

**Migrate FIRST, deploy SECOND — always, including on every redeploy.** This has
already bitten us once (Aug 16): the migration failed, the deploy succeeded, and
prod served code reading a column the schema did not have. It looked healthy,
because health checks and unauthenticated requests never reach `get_current_user`,
so nothing 500s until a real user signs in. **A 200 from the health endpoint is not
evidence a deploy is healthy when the broken path is behind auth.**

The container runs only uvicorn — migrations are a separate step, run from your
machine through the Cloud SQL Auth Proxy. The binary is **not on PATH**; it lives at
`~/cloud-sql-proxy`, and it must listen on **5433** because local Postgres 14 owns
5432 on Jose's Mac.
```bash
~/cloud-sql-proxy --port 5433 $PROJECT:$REGION:chirp-db &
```

> **GOTCHA #2 — you cannot regex the prod DATABASE_URL into a local one.** The
> secret is in Cloud Run's unix-socket form (`...@/chirp?host=/cloudsql/...`, see
> gotcha #1), which contains no `host:port` to substitute. A `sed` that assumes one
> silently matches nothing, and asyncpg then tries a socket path that does not exist
> on a Mac — the failure reads like a permissions problem, not a URL problem.
> **Decompose it and rebuild**: pull the password out of the secret, then assemble a
> fresh `127.0.0.1:5433` URL.

The second line is not optional: it is what turns "the substitution matched nothing"
from a confusing asyncpg socket error into an immediate, obvious stop.
```bash
export PGURL=$(gcloud secrets versions access latest --secret=DATABASE_URL)
export PGPASS=$(printf '%s' "$PGURL" | sed -E 's|^.*://chirp:([^@]+)@.*$|\1|')
[ -n "$PGPASS" ] && [ "$PGPASS" != "$PGURL" ] && echo EXTRACT-OK || echo EXTRACT-FAILED-STOP-HERE
cd backend
DATABASE_URL="postgresql+asyncpg://chirp:${PGPASS}@127.0.0.1:5433/chirp" .venv/bin/alembic upgrade head
DATABASE_URL="postgresql+asyncpg://chirp:${PGPASS}@127.0.0.1:5433/chirp" .venv/bin/alembic current
```
Run `alembic current` and read the revision it prints. **Do not read "no errors" as
"applied"** — that is the other half of the Aug 16 incident.

## 6. FIRST deploy to Cloud Run

This is the **initial** deploy only — it is the one time the env block is empty and
`--set-env-vars` is the right flag. Replace `YOUR_APP_ORIGIN` with the real web
origin before running it. **For every deploy after this one, use section 7 instead.**
```bash
cd backend
gcloud run deploy chirp-api --source . --region=$REGION --allow-unauthenticated \
  --add-cloudsql-instances=$PROJECT:$REGION:chirp-db \
  --vpc-connector=chirp-vpc \
  --set-secrets=DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest \
  --set-env-vars=ENV=production,AUTH_MODE=firebase,FIREBASE_PROJECT_ID=$PROJECT,CORS_ORIGINS='["https://YOUR_APP_ORIGIN"]'
```

## 7. Redeploying (the everyday command)

Migrate first (section 5). Then ship code with **no env flags at all**:
```bash
cd backend
gcloud run deploy chirp-api --source . --region=$REGION
```
Env vars, secrets, Cloud SQL instances and the VPC connector **all persist** across
a `--source` deploy. Carrying them again buys nothing and risks everything.

> **GOTCHA #3 — `--set-env-vars` REPLACES the whole env block; it does not merge.**
> Pasting section 6's line as a redeploy resets `CORS_ORIGINS` to the literal string
> `https://YOUR_APP_ORIGIN` and re-breaks phone login (board c64), and blanks every
> env var added since this doc was written. Worse, it **fails in the browser, not at
> deploy time**: the deploy reports success and the bug surfaces later as "sign-up
> does nothing". **To change one env var, use `--update-env-vars`**, which merges:
> ```bash
> gcloud run services update chirp-api --region=$REGION \
>   --update-env-vars=CORS_ORIGINS='["https://chirps-prod.web.app"]'
> ```
> Reach for `--set-env-vars` only when you genuinely intend to clear everything you
> did not list.

After any redeploy, verify with a **real signed-in request**, not the health
endpoint — see the warning in section 5.

> **GOTCHA #4 — the production safety guard (SECURITY-REVIEW finding 5).** When
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

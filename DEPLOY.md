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

The password is **generated, not typed**. A literal placeholder here is the worst
kind of doc bug: pasting it succeeds, and prod is left with a guessable password
that nothing ever complains about. Keep `$DBPASS` in this same shell — step 4
needs it.
```bash
gcloud sql instances create chirp-db --database-version=POSTGRES_16 \
  --tier=db-f1-micro --region=$REGION
gcloud sql databases create chirp --instance=chirp-db
export DBPASS=$(openssl rand -base64 24 | tr -d '/+=')
gcloud sql users create chirp --instance=chirp-db --password="$DBPASS"
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
Cloud Run reaches private Redis only through a Serverless VPC connector. Step 4
reads the instance's internal IP back out, so there is nothing to copy down here:
```bash
gcloud redis instances create chirp-redis --size=1 --region=$REGION --redis-version=redis_7_0
gcloud compute networks vpc-access connectors create chirp-vpc \
  --region=$REGION --range=10.8.0.0/28
```
Redis is fan-out only (never storage) — the app already degrades gracefully if
Redis is down, so this can come second. **Not provisioned on prod today** (board
c61), which is why the three websocket fan-out tests skip locally (c92).

## 3. Firebase Auth
Do `SETUP-FIREBASE.md` first (Email + Google + Apple). Use the SAME GCP project so
the backend can verify tokens via Application Default Credentials (the Cloud Run
service account) — no service-account key file needed. You only need
`FIREBASE_PROJECT_ID` set on the backend.

## 4. Secrets → Secret Manager
Put anything with a credential in Secret Manager, not plain env. Both values are
built from variables, so the real password is typed exactly zero times and the
Redis IP is captured rather than hand-copied — a literal `INTERNAL_IP` in this
secret does not error, it just silently disables realtime fan-out.
```bash
printf 'postgresql+asyncpg://chirp:%s@/chirp?host=/cloudsql/%s:%s:chirp-db' "$DBPASS" "$PROJECT" "$REGION" \
  | gcloud secrets create DATABASE_URL --data-file=-
export REDIS_HOST=$(gcloud redis instances describe chirp-redis --region=$REGION --format='value(host)')
printf 'redis://%s:6379/0' "$REDIS_HOST" | gcloud secrets create REDIS_URL --data-file=-
```
If you are back in a fresh shell and `$DBPASS` is gone, recover it from the secret
with the decompose recipe in section 5 rather than guessing.

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

## 8. Permanent-media reconciliation job

`python -m app.jobs.media_reconcile` compares objects below `posts/` with every
value still referenced by `posts.media_urls`. It is a dry run unless `--delete` is
explicitly supplied. This job must never use the API runtime service account:
`chirp-api-run` deliberately cannot delete `posts/` objects, and its existing
`tmp/`-only IAM condition must remain unchanged.

The reconciler requires a dedicated identity and a dedicated read-only database
credential. Do not reuse the application's `DATABASE_URL`: doing so would give a
compromised cleanup container the application's write privileges even though the
current job code only issues a `SELECT`.

The commands in this section change production infrastructure. They are manager
steps, not part of a normal backend deploy. Run them from a reviewed shell with the
same `PROJECT=chirps-prod` and `REGION=us-central1` values used above.

Create the database login through the Cloud SQL proxy from section 5. A hexadecimal
password is used so it can be embedded in a URL without percent-encoding ambiguity.
The role can connect, use the public schema, and read only the one column the job
queries.

```bash
export RECONCILE_DBPASS=$(openssl rand -hex 24)
printf "CREATE ROLE chirp_media_reconcile LOGIN PASSWORD '%s';\n" "$RECONCILE_DBPASS" | PGPASSWORD="$PGPASS" psql -h 127.0.0.1 -p 5433 -U chirp -d chirp
PGPASSWORD="$PGPASS" psql -h 127.0.0.1 -p 5433 -U chirp -d chirp -c "GRANT CONNECT ON DATABASE chirp TO chirp_media_reconcile"
PGPASSWORD="$PGPASS" psql -h 127.0.0.1 -p 5433 -U chirp -d chirp -c "GRANT USAGE ON SCHEMA public TO chirp_media_reconcile"
PGPASSWORD="$PGPASS" psql -h 127.0.0.1 -p 5433 -U chirp -d chirp -c "GRANT SELECT (media_urls) ON TABLE posts TO chirp_media_reconcile"
printf 'postgresql+asyncpg://chirp_media_reconcile:%s@/chirp?host=/cloudsql/%s:%s:chirp-db' "$RECONCILE_DBPASS" "$PROJECT" "$REGION" | gcloud secrets create MEDIA_RECONCILE_DATABASE_URL --project=$PROJECT --data-file=-
unset RECONCILE_DBPASS
```

Create a dedicated service account. Use custom roles because the predefined
Storage object-admin roles also authorize object creation and replacement. Listing
is bucket-scoped and therefore has a separate unconditioned binding; deletion is
the only object mutation granted, and its condition is restricted to `posts/`.

```bash
gcloud iam service-accounts create chirp-media-reconcile --project=$PROJECT --display-name="Chirp media reconciler"
gcloud iam roles create chirpMediaObjectLister --project=$PROJECT --title="Chirp media object lister" --permissions=storage.objects.list --stage=GA
gcloud iam roles create chirpMediaPostsDeleter --project=$PROJECT --title="Chirp posts object deleter" --permissions=storage.objects.delete --stage=GA
gcloud storage buckets add-iam-policy-binding gs://chirps-prod-media --member=serviceAccount:chirp-media-reconcile@${PROJECT}.iam.gserviceaccount.com --role=projects/${PROJECT}/roles/chirpMediaObjectLister
gcloud storage buckets add-iam-policy-binding gs://chirps-prod-media --member=serviceAccount:chirp-media-reconcile@${PROJECT}.iam.gserviceaccount.com --role=projects/${PROJECT}/roles/chirpMediaPostsDeleter --condition='title=posts-prefix-only,description=c153 delete only below posts/,expression=resource.name.startsWith("projects/_/buckets/chirps-prod-media/objects/posts/")'
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:chirp-media-reconcile@${PROJECT}.iam.gserviceaccount.com --role=roles/cloudsql.client
gcloud secrets add-iam-policy-binding MEDIA_RECONCILE_DATABASE_URL --project=$PROJECT --member=serviceAccount:chirp-media-reconcile@${PROJECT}.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor
```

Pin the job to the exact image currently serving the API, rather than a mutable tag.
The stored job definition remains dry-run-only: it has neither `--delete` nor a
scheduler, uses one task, and does not retry automatically.

```bash
export RECONCILE_IMAGE=$(gcloud run services describe chirp-api --region=$REGION --project=$PROJECT --format='value(spec.template.spec.containers[0].image)')
printf '%s\n' "$RECONCILE_IMAGE"
gcloud run jobs create chirp-media-reconcile --project=$PROJECT --region=$REGION --image="$RECONCILE_IMAGE" --service-account=chirp-media-reconcile@${PROJECT}.iam.gserviceaccount.com --set-cloudsql-instances=${PROJECT}:${REGION}:chirp-db --set-secrets=DATABASE_URL=MEDIA_RECONCILE_DATABASE_URL:latest --set-env-vars=ENV=production,MEDIA_BUCKET_NAME=chirps-prod-media --command=python --args=-m,app.jobs.media_reconcile --tasks=1 --max-retries=0 --task-timeout=10m
```

Before the first execution, classify legacy values. The last count combines valid
references to another bucket with shapes that need manual inspection; classify each
of those rows before permitting deletion. Any value the dry run itself reports as
unparsed is a stop signal: teach the resolver that shape first. A zero `our_bucket`
count while media URLs exist is also a stop signal and normally means the job points
at the wrong bucket.

```sql
SELECT
  count(*) AS total,
  count(*) FILTER (WHERE media_url LIKE 'https://storage.googleapis.com/chirps-prod-media/%') AS canonical,
  count(*) FILTER (WHERE media_url LIKE '%chirps-prod-media%') AS our_bucket,
  count(*) FILTER (WHERE media_url NOT LIKE '%chirps-prod-media%') AS noncanonical_or_foreign
FROM posts
CROSS JOIN LATERAL unnest(coalesce(media_urls, ARRAY[]::text[])) AS media_url;
```

The manager's first pass is dry-run only. Execute it, wait for success, and read the
complete logs. Record `scanned`, `referenced`, `too_young`, `eligible`, unresolved
values, raw-match protections, and every proposed object name. Independently compare
that list with the database query and `posts/` inventory. Also verify the runner can
list the bucket but cannot create or replace a `posts/` object, cannot access `tmp/`,
and that `chirp-api-run` still cannot delete from `posts/`.

```bash
gcloud run jobs execute chirp-media-reconcile --project=$PROJECT --region=$REGION --wait
```

Do not add a schedule or change the stored job to destructive mode. An actual cleanup
requires explicit manager approval of that exact dry-run output. After approval, use
a one-off execution override so the persistent job remains safe by default, then
inspect the execution logs and rerun the normal dry run to prove the approved objects
are gone and no additional candidates appeared.

```bash
gcloud run jobs execute chirp-media-reconcile --project=$PROJECT --region=$REGION --args=-m,app.jobs.media_reconcile,--delete --wait
```

## Fast path for go-live testing (before full GCP)
The board's "shared backend" blocker doesn't strictly need Cloud Run day one — you
can run the backend locally and expose it with `ngrok http 8000`, point the app's
api base URL at the ngrok URL, and both devs integrate against it. Move to Cloud Run
once auth + feed are wired. (Keep `ENV=local` only for private/ngrok, never a public deploy.)

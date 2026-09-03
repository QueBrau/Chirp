# Chirp — GCP Deploy Guide (backend)

Deploys `backend/` (FastAPI) to **Cloud Run**, with **Cloud SQL** (Postgres 16),
**Memorystore** (Redis), **Secret Manager**, and **Firebase Auth**. The Dockerfile
is already Cloud Run-ready (listens on `$PORT`, uvicorn factory). Pairs with
`SETUP-FIREBASE.md` (auth) and the go-live board cards.

Set once (`PROJECT` is your GCP project id):
```bash
export PROJECT=chirps-prod
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

Cloud Run reaches private Redis only through a Serverless VPC connector. Production
is already provisioned; these read-only commands are the normal verification path:

```bash
gcloud compute networks vpc-access connectors describe chirp-vpc --project=$PROJECT --region=$REGION
gcloud redis instances describe chirp-redis --project=$PROJECT --region=$REGION
```

The production shape verified on Aug 24 is `chirp-vpc` on the default network,
`10.8.0.0/28`, `e2-micro`, min 2/max 3 instances, and `chirp-redis` Basic tier,
1 GiB, Redis 7.0. Both resources are `READY`. The connector is deliberately capped
at three instances because connectors do not scale back in after scaling out.

The following creation commands are first-provisioning reference only. Do not rerun
them against `chirps-prod`; the names already exist. Creating the connector first
reserves its requested range before Memorystore chooses its own private block:

```bash
gcloud compute networks vpc-access connectors create chirp-vpc \
  --project=$PROJECT --region=$REGION --network=default --range=10.8.0.0/28 \
  --machine-type=e2-micro --min-instances=2 --max-instances=3
gcloud redis instances create chirp-redis \
  --project=$PROJECT --region=$REGION --network=default --tier=basic --size=1 \
  --redis-version=redis_7_0
```
Redis is fan-out only (never storage) — the app already degrades gracefully if
Redis is down. c63 bound both resources to `chirp-api-00030-m97` and proved a real
Firebase-authenticated HTTP-message-to-WebSocket round-trip. The on-demand estimate
is approximately $35.77/month for Redis plus $12.23/month for the connector at its
two-instance floor: about $48/month before network transfer. Three connector
instances raise that estimate to roughly $54/month.

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
  | gcloud secrets create DATABASE_URL --project=$PROJECT --data-file=-
export REDIS_HOST=$(gcloud redis instances describe chirp-redis --project=$PROJECT --region=$REGION --format='value(host)')
printf 'redis://%s:6379/0' "$REDIS_HOST" | gcloud secrets create REDIS_URL --project=$PROJECT --data-file=-
gcloud secrets add-iam-policy-binding REDIS_URL --project=$PROJECT \
  --member=serviceAccount:chirp-api-run@${PROJECT}.iam.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor
```
If you are back in a fresh shell and `$DBPASS` is gone, recover it from the secret
with the decompose recipe in section 5 rather than guessing.

`REDIS_URL` version 1 already exists in production and points at the discovered
Memorystore host. c63 used the merge-safe update below; `--update-secrets` preserves
every existing secret mount, while `--set-secrets` would replace them:

```bash
gcloud run services update chirp-api --project=$PROJECT --region=$REGION \
  --vpc-connector=chirp-vpc \
  --update-secrets=REDIS_URL=REDIS_URL:latest
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

`--timeout=3600` below is load-bearing, on this service and on `chirp-ws` alike
(this file only carries the `chirp-api` deploy; `chirp-ws`'s lives in
`INFRA-PRIVATE.html`). Cloud Run's default request timeout is 300s, and it
silently severs every open WebSocket at exactly that mark — invisible until you
measure it, because nothing about a healthy-looking deploy tells you sockets are
dying five minutes in. c247 measured it directly: 17 of 30 upgrades across 14 days
cut at 301.001919s / 301.001959s / 301.000626s, identical to the millisecond
across different days and revisions, and confirmed the fix live afterward
(`chirp-api-00041-tjt` and `chirp-ws-00006-vb8` both report `timeoutSeconds=3600`).
```bash
cd backend
gcloud run deploy chirp-api --source . --region=$REGION --allow-unauthenticated \
  --add-cloudsql-instances=$PROJECT:$REGION:chirp-db \
  --vpc-connector=chirp-vpc \
  --timeout=3600 \
  --set-secrets=DATABASE_URL=DATABASE_URL:latest,REDIS_URL=REDIS_URL:latest \
  --set-env-vars=ENV=production,AUTH_MODE=firebase,FIREBASE_PROJECT_ID=$PROJECT,CORS_ORIGINS='["https://YOUR_APP_ORIGIN"]'
```

## 7. Redeploying (the everyday command)

Migrate first (section 5). Then ship code with **no env flags at all**, but do
carry `--timeout=3600`:
```bash
cd backend
gcloud run deploy chirp-api --source . --region=$REGION --timeout=3600
```
Env vars, secrets, Cloud SQL instances and the VPC connector **all persist** across
a `--source` deploy — c247 re-confirmed this live, not just from gcloud's docs: all
15 env vars, including `EMAIL_FROM` and `EMAIL_PROVIDER`, survived an everyday
`--source` redeploy untouched. Carrying them again buys nothing and risks
everything. `--timeout=3600` is the one flag worth pasting explicitly anyway: it is
the setting c247 had to restore after a real production incident (every WebSocket
silently cut at 301s, see section 6), so it stays in the command text rather than
resting on persistence alone.

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

## Coordinated window (schema-rename deploys)

Sections 5 and 7 above assume migrate and redeploy can each be done whenever —
prod tolerates old code running against a newly-migrated schema because a normal
migration only ever *adds*. A migration that **renames or drops** something old
code still reads breaks that assumption: old code cannot read the new names, new
code cannot read the old ones, and there is no ordering of "migrate" then "wait"
then "redeploy" that avoids an error gap in between. c179's migration 0022
(`yaks`/`yak_votes` → `chirps`/`chirp_votes`, plus the `content_reports` /
`moderation_actions` target_type backfill) is the worked example below, and it was
not alone on the chain — 0019, 0020 and 0021 rode the same window (0019 → 0022),
not just the rename at the tip.

**This specific window has since closed** — prod's `alembic_version` was verified
at `0028` by Aug 28 and moved to `0029` on Aug 30 (c260/c237), and alembic only
reaches a revision by applying every ancestor in its chain, so 0019-0022 are long
applied. The steps and abort criteria below remain the playbook for the *next*
rename-shaped migration — read the 0022 references as a worked example, not a live
TODO.

Treat migrate + redeploy as **one window**, at a quiet hour, run start-to-finish
without gaps between the steps below. Do not migrate and then walk away.

1. **(Jose) Migrate.** Start the Cloud SQL Auth Proxy and run
   `alembic upgrade head` through it exactly as section 5 describes — proxy on
   port 5433, `DATABASE_URL` decomposed from the `DATABASE_URL` secret, never a
   typed placeholder. The exact proxy command and the ready-to-paste
   `postgresql+asyncpg://...@localhost:5433/chirp` URL (real password included)
   live in `INFRA-PRIVATE.html#proxy` and `#migrate` — this file intentionally
   never inlines that password. Then run `alembic current` and **read the
   revision it prints** — confirm it says `0022 (head)` before moving to step 2.
   Section 5's Aug 16 lesson still applies: "no errors" is not "applied".
2. **(Jose → manager) Signal.** Say the migration landed and `alembic current`
   read back `0022`. This is what starts the clock on the error gap below —
   step 3 should follow within minutes, not whenever the manager gets to it.
3. **(manager) Redeploy the API.** Section 7's everyday command, no env flags but
   with `--timeout=3600`:
   `cd backend && gcloud run deploy chirp-api --source . --region=$REGION --timeout=3600`.
   Between step 1 finishing and this step finishing, the live backend is old
   code serving against renamed tables — every request that touches
   yaks/chirps, content_reports, moderation_actions, house_ballots or
   role_terms 500s or 404s depending what the old code was trying to do. **This
   gap is unavoidable with a table rename and is not itself a signal to abort**
   — see the abort criteria below for what actually is.
4. **(manager) Rebuild and deploy the web client.**
   `cd web && npm run build && firebase deploy --only hosting`. The compiled
   client still calls `/campuses/{id}/yaks` until this runs, so web stays
   broken on the new API even after step 3 completes — this is not optional
   just because the API is already up.
5. **(manager) Verify.** `scripts/deploy-verify --base-url <chirp-api service
   URL from INFRA-PRIVATE.html#cloudrun>` — see below for what it checks. Follow
   with a real signed-in request through the app per the section 5 / gotcha #4
   warning: a 200 from health, or from this script's control checks alone, is
   not evidence the deploy is healthy on the paths that matter.

**Abort / rollback.** 0022's downgrade is real and tested (c179: migration
up → down → up run against a database holding a real `yak` report row; the
downgrade restores `yak_id` and all four `yak`-named constraints exactly). If
step 3 fails outright, or does not complete within a few minutes of step 1,
that — not the expected error gap itself — is the abort signal: downgrade back
through the same proxy session (`alembic downgrade -1` from `0022`, repeat down
to `0013` — prod's last verified head per `INFRA-PRIVATE.html#latest` — if the
whole 0019-0022 window needs to unwind) so the schema matches whatever Cloud Run
revision is still serving traffic, then retry the window from step 1 once the
redeploy problem is fixed. Do not leave the database migrated ahead of the code
running against it — that is the exact state this section exists to keep brief
instead of open-ended.

### scripts/deploy-verify

Run after step 4, pointed at whatever you just deployed. It probes four things
over plain HTTP — no credentials of its own, so it never needs prod access to
exist or to be rehearsed locally — and prints (never runs) the two manual proof
steps that do need real credentials:

- **(a)** a real auth-gated route with no token → expects `401`. Deliberately
  **not** `/healthz` — Google's `*.run.app` frontend intercepts that exact path
  and answers with its own 404 before the request reaches the container
  (`INFRA-PRIVATE.html#cloudrun` gotcha), which would misreport a healthy
  deploy as down.
- **(b)** a route that has never existed, as a control — expects `404`. If this
  is not 404, the target isn't answering normal FastAPI routing at all (wrong
  host, a proxy, a maintenance page) and nothing else the script reports can be
  trusted.
- **(c)** the route-swap hinge itself: `/campuses/{id}/yaks` must be fully gone
  (`404` — the router module that served it no longer exists), and
  `/campuses/{id}/chirps` must still be routed (`401` unauthenticated, or `200`
  if run with `--bearer`).
- **(d)** prints the exact `gcloud logging read` command for c176's "email
  sent" log-line proof. The send itself stays a manual step (trigger one real
  `.edu` verification through the app, or a bearer-authenticated
  `POST /auth/campus-verification`) — the script only prints the read.
- **(e)** prints the read-only SQL for c184's four URL-column counts
  (`alumni_profiles.linkedin_url`, `job_posts.apply_url`, `events.cover_url`,
  `users.avatar_url`) for Jose to run by hand through the proxy — the script
  never executes it.

Defaults to `http://localhost:8000` so it runs unattended against a local
stack; pass `--base-url` (or `DEPLOY_VERIFY_BASE_URL`) for the prod service URL,
which lives in `INFRA-PRIVATE.html#cloudrun` and is deliberately not hardcoded
here.

**c250: running it bare after a prod deploy produces a total red that looks like a
prod failure and is not.** A manager did exactly that and got `0 passed, 4 failed`
with four `000` status codes — every probe reaching nothing, because the script was
still quietly checking `localhost:8000`. A total red across every probe (`000`
everywhere) means **wrong target**, not a broken deploy — check the `target:` line
the script prints as its second line before you re-run it or start diagnosing.
That is different from a cold-start flake (a partial red against a real URL, e.g.
3/1) — don't conflate the two. The correct invocation after any prod deploy is
`scripts/deploy-verify --base-url <service URL>`.

## Env var reference (Settings → env)
| env var | required | example |
|---|---|---|
| `DATABASE_URL` | yes | asyncpg unix-socket URL (gotcha #1) |
| `REDIS_URL` | for realtime | `redis://10.x.x.x:6379/0` |
| `ENV` | prod | `production` (forces firebase + real CORS) |
| `AUTH_MODE` | prod | `firebase` |
| `FIREBASE_PROJECT_ID` | prod | `chirps-prod` |
| `CORS_ORIGINS` | prod | `["https://app.chirp..."]` (JSON array) |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | milestone 8 | — |

## Media privacy cutover gate (closed)

**This gate is closed. If you find an older copy of this file, or a paste buffer,
telling you to leave `chirps-prod-media` public — ignore it. That instruction is
stale and is exactly the paste-trap this section now exists to stop.**

What the gate was: the signed-media route and `MEDIA_SIGNING_SECRET` were deployed
and survived the c63 revision update, but `chirps-prod-media` intentionally stayed
publicly readable until two independent conditions were met — a signed-media
capability URL had to render a real uploaded photo through the shared EAS app on a
physical device, and Jose had to approve the exact privacy mutation after seeing
that proof.

What is proven, per the board (this file has not independently re-verified the live
bucket ACLs — treat the following as the board's record, not a fresh check):
Public Access Prevention is enforced and the `allUsers` grant is removed, with a
`posts/`-conditioned read grant added for the runtime service account (Jose applied
the flip; c140, Aug 24). It was verified propagation-safe at the time: a capability
URL served the exact bytes, re-checked 60s later; a direct GCS URL 403s; the app
renders through the capability route. c244 (Aug 31) closes what remained of the
original "unbounded media storage" concern this gate was guarding against: `tmp/`
objects auto-delete at 1 day, upload-URL minting is rate-limited to 60/10min per
user, and the object size range is enforced in the signed URL itself (c133).

If you need current-moment proof rather than the board's record, re-run c140's two
checks: a capability URL still renders, and a direct unauthenticated object URL
still does not.

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

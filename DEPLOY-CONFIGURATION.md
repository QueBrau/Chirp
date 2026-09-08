# Intended deployment and paired releases (c362)

[infra/deployment.json](infra/deployment.json) is the reviewed source for the API
and WebSocket service settings, public client endpoints, Secret Manager references
and connection capacity policy. `scripts/deployment-config` compares that source
with read-only cloud metadata or prints commands derived from it. It never builds,
deploys, changes traffic, migrates, retrieves secret payloads or executes a job.

A release record supplies the **independently reviewed** immutable image, target
revision names and schema head. Observed cloud state must never be copied into the
expected release merely to turn a failure green. The tool and the pool regression
consume the same source; changing a cap requires reviewing its rollout arithmetic.

## Scope and current architecture

Both services still run the complete FastAPI application under the same runtime
service account. The split manages HTTP/socket concurrency; it is not a security
boundary. API-specific Stripe and email secret bindings remain API-specific. The
shared account's permissions have not been narrowed. Neither this file nor the
checker proves least privilege, resolved secret-version parity, every environment
variable, an effective deployed image's Python defaults, or runtime worker behavior.
The checked Dockerfile starts one uvicorn process; the generated service commands
make its `WEB_CONCURRENCY=1` and pool values explicit. Command/argument overrides or
additional containers produce incomplete evidence instead of trusting this model.
Jobs must match their declared `python -m app.jobs...` entrypoint and have a pinned
image reference. Unknown wrappers, worker overrides or image identity fail proof.

The intended mode is automatic scaling. The checker projects and validates both
`scalingMode` and `manualInstanceCount`, and commands include `--scaling auto`.
Manual scaling bypasses revision min/max limits and invalidates this capacity
model; it is reported as drift, not covered by the temporary overshoot caveat.
See [manual scaling](https://docs.cloud.google.com/run/docs/configuring/services/manual-scaling).

The source preserves the service sizes observed in the Sep 7 audit: revision caps,
minimums, CPU, memory, concurrency, request timeouts, service-level caps, Cloud SQL
attachment, VPC routing, runtime identity and existing secret references. It owns
only the listed settings. Other environment variables and secrets persist through
merge-only update flags. Extra settings outside this allowlist are not audited.

## Read-only inspection and evidence

Run from the repository root. This invocation needs existing metadata-read access;
it does not need a Firebase bearer or database credentials:

```sh
scripts/deployment-config inspect --gcloud "$HOME/google-cloud-sdk/bin/gcloud" --report /tmp/chirp-config-observed.json
```

Inspection records UTC time, the intended-file SHA-256, template checks, every
revision referenced by resolved traffic or latest-created/latest-ready metadata,
resolved image digests, traffic percentages/tags, published endpoint aliases,
job pool/parallelism estimates and connection envelopes. The services are reread
after collection; a changing snapshot cannot pass. Reads use explicit project and
region, field projections, filtered nonsecret environment rows and secret **refs**.
Provider error bodies, credential values and arbitrary annotations are omitted.
Each gcloud subprocess has a 30-second deadline. No HTTP request is made here.

The numeric Cloud Run URL used by the mobile app and the hashed canonical
`status.url` are accepted as aliases only if that actual service publishes both
in `run.googleapis.com/urls`. The authenticated verifier still receives the
canonical origins, as its own contract requires.

`inspect` has no expected release, compiled-client observation or SQL observation,
so it cannot return `CONFIG_MATCH`. Exit 1 with `NOT_PROVEN` is expected when only
that evidence is absent. Actual mismatches return `DRIFT`. Exit 2 means invalid
input, failed reads or incomplete/malformed provider data. The only successful
comparison verdict/exit pair is `CONFIG_MATCH`/0. `TRANSITION` returns 1 and is
never steady-state acceptance. A printed command plan returns 0 with
`PRINT_ONLY_NOT_EXECUTED`; it is not comparison evidence.

For a release, put these nonsecret fields in a local JSON file. The following is a
shape example, **not an approved image, revision, build or SQL observation**:

```json
{
  "image": "us-central1-docker.pkg.dev/chirps-prod/cloud-run-source-deploy/chirp-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "revisions": {"api": "chirp-api-release-label", "ws": "chirp-ws-release-label"},
  "schema_head": "reviewed_schema_head",
  "database_observation": {
    "observed_at": "2026-09-07T12:00:00Z",
    "max_connections": 100,
    "superuser_reserved_connections": 3,
    "reserved_connections": 0
  },
  "client_build": {
    "build_id": "actual-build-artifact-id",
    "observed_at": "2026-09-07T12:00:00Z",
    "api_url": "https://chirp-api-593616178468.us-central1.run.app",
    "ws_url": "wss://chirp-ws-593616178468.us-central1.run.app/ws"
  }
}
```

Use database `SHOW`/`current_setting` results from an authorized connection and
endpoint values inspected in the identified compiled mobile artifact. Repository
`eas.json` and source defaults alone do not establish what an installed build
contains; remote EAS environment variables can override the checked-in defaults.
The tool compares these **operator-supplied observations** with the intended
values and rejects missing, stale, future or mismatched observations. It does not
independently open the database or download/inspect a build. Keep observation
provenance with the release record. Default evidence freshness is in the source.

```sh
scripts/deployment-config check --release /tmp/chirp-release.json --gcloud "$HOME/google-cloud-sdk/bin/gcloud" --report /tmp/chirp-config-release.json
```

Even `CONFIG_MATCH` is configuration evidence only. Run the generated c361
command and require `AUTHENTICATED_READY` separately; neither tool proves a
WebSocket upgrade, Redis fan-out, device behavior, capacity under load or cost.

## Pool policy and rollout constraints

Each process can open `DB_POOL_SIZE + DB_MAX_OVERFLOW` database connections.
Multiply by workers, revision instance limits and every overlapping revision.
For jobs, additionally multiply by parallel tasks and overlapping executions.
A task retry is sequential within that task, so it is not another simultaneous
pool; a separate execution can overlap. The declared execution limit is an
operator policy, not a Cloud Run setting enforced by this tool.

The report derives three scenarios: steady services plus jobs; one service with
old/new revisions while the other is steady and jobs are quiescent; and both
services overlapping with jobs. It includes PostgreSQL reserved slots, an
operator/migration slot and a separately declared contingency allowance. The
current source produces **68 / 98 / 112** against an **assumed 100** connection
capacity. The last scenario fails. These totals include ten contingency slots;
they are not measurements of open connections. The extra allowance is planning
headroom, not a bound on Cloud Run overshoot.

Missing pool environment settings are calculated from the actual checked-out
`backend/app/config.py` defaults and explicitly labelled **inferred from checkout,
not image**. They prevent `CONFIG_MATCH`; old job images may have different
compiled defaults. The observed revision envelope includes tagged/staged revisions
visible in the collected metadata and never discounts a zero-percent tag.
Unlisted draining/warm revisions and future job executions are not enumerated.
Unknown jobs, missing definitions or configuration drift prevent acceptance.

Cloud Run can temporarily exceed configured maximum instances. Revision limits
apply to each revision, and tagged revisions have additional service-cap caveats.
Consequently none of these sums guarantees a hard connection limit or availability.
Use database/instance telemetry during the release, and stop if there is no safe
headroom. See [maximum instances](https://docs.cloud.google.com/run/docs/configuring/max-instances)
and [autoscaling behavior](https://docs.cloud.google.com/run/docs/about-instance-autoscaling).

For the stated rollout scenario, the operator must keep jobs quiescent for the
whole window, prevent overlapping manual/scheduled executions, and confirm the
previous service's old revision has drained to zero instances before staging the
second service. Zero HTTP traffic is insufficient: existing sockets can continue
and idle pools may remain open. Do not infer drain solely from a timer or the
current traffic allocation. This checker does not pause jobs or prove drain.

An optional `rollout` object records `started_at`, `ends_at`, `jobs_quiescent: true`
and `previous: {"api": {"revision": "...", "image": "..."}, "ws": {...}}`.
Only those exact predecessor revisions/digests can be classified as an expected
transition inside the bounded window. A service still wholly on its declared old
revision is allowed while the other rolls out; overlapping old/new revisions on
both services violates the policy. An expired/future/overlong window, unknown
image/revision, or tagged predecessor remains drift. The jobs field is a dated
operator attestation, not an automatic inventory of running executions.

## Complete paired-service deployment procedure

Production mutations remain operator/manager steps. This implementation executed
none. The following sequence applies to the existing pair; it does not provision
a new project, add IAM grants or replace jobs.

1. Select the reviewed backend commit and build **once** using the existing build
   workflow/Artifact Registry repository named in the intended source. Record its
   immutable digest and the image's Alembic head. Use that same `repository@sha256`
   value for both services. Running `--source` separately twice does not establish
   identical images. Choose unique future revision suffixes in the release record.
2. Review the current inspection, rollback revision/digest records, database
   capacity evidence, schema compatibility and job/drain controls. Fix unexplained
   drift through its scoped review before using the generated settings to overwrite
   it. Use a coordinated window for breaking migrations. Apply migrations and
   verify `alembic current` as [DEPLOY.md](DEPLOY.md) requires before serving new code.
3. Render the exact pair of stage/promote commands and the authenticated verifier
   arguments from the same file. Read the output; do not pipe it to a shell:

```sh
scripts/deployment-config plan --release /tmp/chirp-release.json --gcloud "$HOME/google-cloud-sdk/bin/gcloud" --report /tmp/chirp-paired-plan.json
```

4. With jobs quiescent, execute the API stage command after operator review. It
   deploys the pinned image with `--no-traffic`, the chosen revision name and all
   owned sizing/network settings. It uses `--update-env-vars` and
   `--update-secrets`, preserving unrelated settings. It does not change IAM.
   Inspect revision readiness and the intended diff, then execute the API promote
   command to route 100% to that explicit revision. Do not proceed on a failed or
   unexpected stage. Traffic can change while old requests remain in flight.
5. Confirm the API predecessor has drained to zero instances and connection
   headroom remains safe. Then execute the **WS** stage and promote commands with
   the same image. Keep the full configured request timeout; clients reconnect as
   sockets age out. Confirm the WS predecessor drains before releasing job controls.
   Tags/old revisions that preserve capacity need explicit accounting. The tool
   will not silently remove tags or change rollback policy.
6. Reinspect after convergence. Set `API_CANONICAL_ORIGIN` and
   `WS_CANONICAL_ORIGIN` from the sanitized service evidence, plus known
   `QA_USER_ID`/`QA_CAMPUS_ID`. Load the short-lived bearer into
   `DEPLOY_VERIFY_BEARER` using the approved credential source. Execute the printed
   verifier command; its digests, revisions, service names, project and schema are
   derived from this release. Follow [DEPLOY-VERIFICATION.md](DEPLOY-VERIFICATION.md),
   then unset the bearer. Require authenticated readiness on **both** services.
7. Record the final configuration report, authenticated report, compiled client
   endpoint evidence and separate socket/device evidence. Jobs retain their own
   images and rollout cards; an API/WS deployment does not update them.

If a stage fails, stop with the old serving revision preserved. If a promoted
release fails checks, stop the sequence and use the recorded prior revision only
when it is compatible with the current database schema. A rollback is another
rollout with capacity/drain implications; do not start it while assuming the
previous pool has vanished. Breaking schema changes need their coordinated
recovery plan, not an automatic traffic rollback.

Flags and traffic behavior are grounded in the current primary references:
[gcloud run deploy](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy)
and [rollouts and traffic migration](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration).

## Implementation evidence and remaining live acceptance

The committed [sanitized inspection](infra/evidence/c362-2026-09-08.json) was
captured **2026-09-08 03:03:59 UTC** (Sep 7 locally). The checker found no mismatch
in its managed settings. Both services served the same resolved image digest,
each with 100% traffic on one revision; automatic scaling, job entrypoints and
published client-origin aliases matched. This is a dated observation, not an
expected future release.

Its verdict is deliberately `NOT_PROVEN`: the inspection supplied no independent
expected release, current SQL settings or compiled-client observation. API and
both jobs still lack explicit pool values, so their old image defaults are
unproven. A proposed read-only SQL connection was stopped by automatic approval
review before execution; it rejected extracting credentials from the private
operator file. No retry or alternative credential path was attempted. An operator
can supply a sanitized current settings observation through the documented
release input when authorized. No deployment or database mutation ran.

Final local validation passed **70 configuration/pool cases**, including the
independent review's unresolved-traffic, job-worker/image, blank build ID and
manual-scaling failure cases. The **59 existing c361 CLI cases** also passed in
the same session. The original steady-state pool guard was executed separately:
it passed while the expanded simultaneous rollout/job calculation exceeded the
assumed capacity. The new policy rejects that scenario. An independent reviewer
reran both unchanged falsification scripts after the fixes and accepted the
source. These checks establish local tooling behavior; they do not close the
remaining production release/build/database or device acceptance.

### Approved SQL follow-up — September 8, 2026, 16:00:55 UTC

After explicit approval, a read-only transaction through the existing local
Cloud SQL proxy verified its target and read the numeric PostgreSQL limits:
**100 max connections, 3 superuser reserved, 0 additional reserved**. The separate
[sanitized observation](infra/evidence/c362-sql-2026-09-08.json) matches the policy's
planning values. No database changes ran and no credentials were printed.

This resolves the missing SQL-limit observation at that timestamp; the earlier
inspection and denied attempt above remain historical records. Overall release
and configuration acceptance remains `NOT_PROVEN`: a reviewed release, compiled
client evidence and proof of the old images' inferred pool defaults are still
missing. SQL limits alone are not a new full configuration comparison. Use this
observation only within the checker's freshness window, then obtain a fresh read.

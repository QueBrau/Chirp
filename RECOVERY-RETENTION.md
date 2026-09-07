# Recovery and retention operations (c368, c369)

Owner: Jose/Astra. These cards remain In Progress until the live acceptance evidence is recorded on the board. This first change adds recovery inspection and safer purge execution; deployment, a database restart, a restore drill, and production deletion are separate operator steps.

## Current evidence and scope

Read-only metadata rechecked September 7, 2026 at 15:57 UTC:

- Automated backups are enabled, with 14 retained daily backups. All 15 listed backups (including an older on-demand backup) were successful; the latest completed at 09:47 UTC.
- The PITR enabled flag was absent despite seven configured transaction-log retention days. The recovery-window query reported no backups usable for PITR. Successful daily backups therefore do not establish current point-in-time recovery.
- The latest retention job execution failed on Secret Manager access while using the default compute identity. Its command runs the purge module without arguments. Repairing that permission alone would execute the old unbounded deletion job.

The August 27 restore rehearsal (c215) and August 22 purge success (c69) remain historical evidence. This snapshot does not establish the cause of the later drift. Recheck current state before every operation. Keep raw account metadata, credentials and recovery fixtures outside this public repository.

## Recovery inspection

Run from the repository root with an authenticated gcloud installation:

~~~bash
python3 scripts/recovery-check --project chirps-prod --instance chirp-db
~~~

On Jose's machine, add the executable path if gcloud is absent from PATH:

~~~bash
python3 scripts/recovery-check --project chirps-prod --instance chirp-db --gcloud "$HOME/google-cloud-sdk/bin/gcloud"
~~~

The script makes metadata reads only. It does not access secrets, connect to PostgreSQL, enable APIs, change settings or create a restored instance. Its JSON distinguishes configured retention, the available recovery window, backup freshness and inspection errors. Defaults are a 36-hour maximum automated-backup age, 15-minute maximum recovery lag, and seven configured log days; these are proposed operational checks, not measured recovery guarantees.

Exit 0 means the metadata meets those checks. Exit 1 means a known policy gap; exit 2 means an inspection or input error. Neither a zero exit nor a SUCCESSFUL backup proves a usable restored application. The output always marks restore verification false. Short actual coverage after enabling PITR must be recorded even if the configured seven-day target is met.

## PITR repair and rehearsal

Inspect SQL operation and administrative audit history before attributing the drift to a person or deploy. In particular, restoring an older backup is one possible reason for backup configuration and log history changing; it is not an established explanation for this incident. Google documents that a backup restore resets backup configuration and removes previous PITR log history. [Restore behavior](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/restore)

Enabling PITR on an existing instance restarts it. Plan a maintenance window, record the serving revisions and known authenticated checks, and coordinate with the deployment owner. Do not toggle PITR off and on to diagnose it: missing historical coverage cannot be recreated. [PITR configuration](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/configure-pitr)

The proposed source patch, only after confirming that PITR is still absent, is:

~~~bash
gcloud sql instances patch chirp-db --project=chirps-prod --enable-point-in-time-recovery --retained-transaction-log-days=7
~~~

Preserve the current daily schedule, 14-backup retention, source deletion protection, tier and network configuration. Wait for the operation to complete, verify the source is RUNNABLE, rerun recovery-check, and check authenticated HTTP and WebSocket reconnection. Record any outage interval. Do not declare recovery restored until an actual recovery window exists and a drill succeeds.

For the drill, select a UTC timestamp inside the reported earliest/latest window. Use a new, uniquely named drill instance with a recorded owner and cleanup deadline. A plain current-state clone does not test historical recovery. The operator sets both variables below to the reviewed values before running the command:

~~~bash
: "${CHIRP_DRILL_INSTANCE:?Set the reviewed unique drill instance name}"
: "${CHIRP_DRILL_TIME:?Set an RFC3339 UTC time inside the recovery window}"
[ "$CHIRP_DRILL_INSTANCE" != "chirp-db" ] && gcloud sql instances clone chirp-db "$CHIRP_DRILL_INSTANCE" --project=chirps-prod --point-in-time="$CHIRP_DRILL_TIME"
~~~

PITR creates a new instance. A clone contains production data and copies user credentials and settings, so verify its network boundary and keep production jobs and side effects disconnected. [PITR procedure](https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/pitr), [clone behavior](https://docs.cloud.google.com/sql/docs/postgres/clone-instance)

Validate through a separate proxy/application configuration:

1. Confirm the target identity before connecting. Read the restored migration revision expected at the selected historical timestamp; do not blindly upgrade the clone to today's head before checking the restore.
2. Compare reviewed aggregate counts and referential integrity against known fixture evidence. Check fixtures created before and after the target timestamp. Never log real post bodies, payment amounts, tokens or database credentials.
3. Exercise authenticated membership, feed and message-history reads using the isolated application. Disable external email, payment execution, push, scheduled jobs and production pub/sub access for the drill. Q can independently verify the approved application journey after the owner marks it ready.
4. Record request time, clone-ready time, validation time, application-ready time and the verified data boundary separately. A metadata lag is not measured data loss. Record actual cost and any unresolved limitations.
5. Confirm the unique drill target again, disable deletion protection only on that target if inherited, then delete only that target and verify removal. Never remove source deletion protection. Name the cleanup owner before creating the clone.

Use a short, time-bounded drill; it temporarily adds another database instance and storage. Reprice the actual live tier before scheduling. c368 closes only with a usable current recovery window, restored-data/application proof, cleanup evidence and an agreed recovery procedure. HA remains the separate c287/c363 growth decision.

## Retention execution contract

The new purge CLI defaults to a dry run. Existing no-argument Cloud Run definitions must be deliberately updated after rollout; they will otherwise preview candidates and leave deletion pending. Permanent-media reconciliation is a different job with its own existing approval policy; this change does not operate on GCS objects.

From backend/ with DATABASE_URL already supplied securely for the intended environment:

~~~bash
python -m app.jobs.purge --dry-run --retention-days=30 --batch-size=100 --max-batches=10 --max-seconds=120
~~~

The preview uses SELECT statements in a read-only transaction. It reports aggregate candidate counts, including dependent rows, without content or object identifiers. Capped values are lower bounds rather than total backlog estimates. A live database may change between preview and application; preview is not an approval token for an immutable deletion set.

The destructive mode must be explicit:

~~~bash
python -m app.jobs.purge --apply --retention-days=30 --batch-size=100 --max-batches=10 --max-seconds=120
~~~

Each application batch is independently committed. The batch size applies separately to six deletion reasons: posts, independently expired comments, chirps, post likes, comments removed with a post, and chirp votes. A size of 100 therefore permits at most 600 physical row deletions per batch; the size-25 canary permits at most 150. A parent with many children can require several batches. SQL timeouts and a finite wall-clock budget limit contention. Already committed batches remain committed if a later batch fails. A deadline during commit is an unknown commit outcome, not proof of rollback: inspect the summary and rerun the preview before resuming. Do not restore a database to undo one batch as an automatic response.

The caller-owned Python helper keeps its full-run transaction semantics for existing callers. Use the CLI for operational batch and time limits. Reports and financial/audit history are outside this retention job's deletion scope. Purging database media references does not itself delete GCS objects.

## Job identity repair and rollout

The failure identifies a permission problem, not permission to grant the default compute account broad secret access. Configure a dedicated purge runtime identity and a dedicated database credential. Do not reuse the API application's database login. Cloud Run's job identity needs Secret Accessor on the dedicated purge secret only, plus Cloud SQL Client for the connector. Keep deployment/actAs privileges with the operator. [Job secrets](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets)

Prepare and review these changes together:

- A dedicated database login with CONNECT and schema USAGE, SELECT/DELETE on posts, post_comments, post_likes, chirps and chirp_votes, plus UPDATE(id) on posts, post_comments and chirps for row locks, and UPDATE(score) on chirps for the vote-deletion score trigger from migration 0025. These UPDATE grants are real column-write privileges, so verify the role can change no other columns and use it only in the dedicated job. No ledger, payment, user, key, DDL, role-management or unrelated secret privileges.
- A dedicated Secret Manager secret containing that login's Cloud SQL socket URL. Generate the password without putting it in a command argument or logs; provision using the private operator procedure. Grant access on that secret, not at project scope.
- A dedicated Cloud Run service account, an explicit Cloud SQL attachment, one task and one connection pool slot with no overflow, a reviewed image digest, zero automatic retries during rollout, and a task timeout longer than the CLI budget.
- An inventory of the existing scheduler target and invocation identity. Pause or otherwise coordinate it before replacing the job configuration so an old destructive invocation cannot race the preview. Verify its permissions allow invocation without granting runtime access to other secrets.

Use a reviewed image built from the merged implementation. Updating the API alone does not update a Cloud Run job's image. Set the four variables to the prepared identity, secret version, connection and image digest; no secret value belongs in this command:

~~~bash
: "${CHIRP_PURGE_IDENTITY:?Set the dedicated purge service account email}"
: "${CHIRP_PURGE_SECRET_VERSION:?Set the dedicated secret name and numeric version}"
: "${CHIRP_SQL_CONNECTION:?Set the reviewed Cloud SQL connection name}"
: "${CHIRP_PURGE_IMAGE:?Set the reviewed image pinned by digest}"
gcloud run jobs update chirp-purge --project=chirps-prod --region=us-central1 --service-account="$CHIRP_PURGE_IDENTITY" --image="$CHIRP_PURGE_IMAGE" --set-cloudsql-instances="$CHIRP_SQL_CONNECTION" --set-secrets="DATABASE_URL=$CHIRP_PURGE_SECRET_VERSION" --update-env-vars=DB_POOL_SIZE=1,DB_MAX_OVERFLOW=0,PURGE_RETENTION_DAYS=30 --command=python --args=-m,app.jobs.purge,--dry-run,--batch-size=100,--max-batches=10,--max-seconds=120 --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=180s
~~~

This replacement of secret mappings is intentional for the dedicated purge job; first verify its sole necessary secret is DATABASE_URL. Keep the job's stored arguments in dry-run mode during validation. Inspect the resulting job definition and identity rather than relying on the update command's exit status. The flags are documented in the [job update reference](https://docs.cloud.google.com/sdk/gcloud/reference/run/jobs/update).

Then run the preview and wait for completion:

~~~bash
gcloud run jobs execute chirp-purge --project=chirps-prod --region=us-central1 --wait
~~~

Before the first production application, verify the intended database and retention cutoff, compare candidate counts with a bounded independent read, confirm a usable recovery path, and review the output. Start with one small batch via a one-off execution override; do not switch the recurring schedule to application first. The operator needs run.jobs.runWithOverrides on this job; the scheduler's ordinary invocation permission is insufficient. Keep that extra permission with the operator. [Execution permissions](https://docs.cloud.google.com/run/docs/execute/jobs)

~~~bash
gcloud run jobs execute chirp-purge --project=chirps-prod --region=us-central1 --args=-m,app.jobs.purge,--apply,--batch-size=25,--max-batches=1,--max-seconds=60 --wait
~~~

Check the reported outcome, dependent-row integrity, surviving recent/live fixtures and a repeat preview. Validate denied access to unrelated secrets/tables using the dedicated identity. Only after that proof should the operator set the stored arguments to the reviewed --apply limits and enable the daily schedule. Keep explicit retention settings; a newer image must not silently shorten the policy.

c369 remains open until a scheduled production execution succeeds, repeat execution is safe, least-privilege checks pass and alerting covers failures and overdue completion (coordinate c370). Budget exhaustion is incomplete work, not a clean sweep. Capture counts and cutoff only; content and credentials must not enter the board or logs.

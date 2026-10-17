# c436 operator deletion executor

`app.jobs.account_deletion_executor` is an explicit, single-request operator tool.
It is not mounted in the API, registered as a scheduler, or invoked on import. The
default command path is absent: the operator must choose either `--prepare` or
`--execute`, and execution also requires the exact SHA-256 shown by preparation.
The tool does not activate a deletion hold, change any admission setting, or turn
on a background worker.

## Prerequisites

Before preparing a request, an authorized operator must already have:

- deployed all four account-deletion admission integrations and verified their
  environment settings are enabled: HTTP, WebSocket, delivery, and deferred cleanup;
- drained every process running an older writer revision, and recorded that manual
  verification in the private approval file, including a reference to the
  evidence artifact and its SHA-256 digest;
- committed the durable hold for this exact deletion request, with registered
  external operations, WebSockets, deliveries, and payment attempts drained;
- reviewed the exact BigQuery dated-table set and any permitted probe exclusion,
  and recorded an unexpired analytics freeze attestation. The `ingestion_drained`
  value must be `false`: this executor does not prove that delayed analytics
  ingestion has ceased;
- reviewed the GCS bucket's current versioning and soft-delete settings and the
  exact configured bucket;
- installed the required SDKs and selected ADC explicitly in the operator
  environment. The expected cloud project is pinned in code to `chirps-prod`;
  the analytics dataset is pinned to `chirp_analytics`.

The old-revision drain and analytics freeze are operator attestations, not facts
the CLI can infer. They must describe concrete evidence and the reviewer in the
approval artifact. Admission flags are checked both against that artifact and the
current process settings. The database check separately verifies actual database,
role, schema head `0050`, connection endpoint, and the executor's exact
table/column privilege allowlist. It refuses elevated role attributes, including
replication capability, membership in database-wide privileged roles, table
ownership and unexpected app-table write or trigger privileges. The schema
head is intentionally exact; update this tool after a
future migration.

## Private approval file

Create an owner-only directory (`0700`) and an owner-only JSON file (`0600`). The
file is bound to the exact request, account, database, role and schema. It must have
exactly these fields:

```json
{
  "type": "c436_operator_approval_v1",
  "request_id": "<canonical UUID>",
  "user_id": "<canonical UUID>",
  "database_name": "<expected database>",
  "database_role": "<expected PostgreSQL role>",
  "schema_head": "0050",
  "reviewed_by": "<reviewer identity>",
  "reviewed_at": "<timezone-aware ISO timestamp>",
  "expires_at": "<timezone-aware ISO timestamp, within one hour>",
  "old_revisions_drained": true,
  "old_revisions_evidence_ref": ".ticket-evidence/c436/old-revision-drain.json",
  "old_revisions_evidence_sha256": "<64 lowercase hex characters>",
  "admission_flags": {
    "account_deletion_http_admission_enabled": true,
    "account_deletion_ws_admission_enabled": true,
    "account_deletion_delivery_admission_enabled": true,
    "account_deletion_deferred_cleanup_enabled": true
  },
  "firebase_project": "chirps-prod",
  "analytics_project": "chirps-prod",
  "analytics_dataset": "chirp_analytics",
  "bucket": "<exact configured account-media bucket>",
  "soft_delete_seconds": 604800,
  "analytics_tables": ["run_googleapis_com_stdout_YYYYMMDD"],
  "analytics_exclusions": [],
  "analytics_freeze": {
    "user_id": "<same canonical UUID>",
    "freeze_id": "<reviewed identifier>",
    "expires_at": 1792180000,
    "verified": true,
    "ingestion_drained": false
  }
}
```

Replace every placeholder with reviewed values; `expires_at` in the freeze is a
Unix timestamp in seconds and the example value is illustrative; replace it with
a currently valid reviewed value. The old-revision evidence reference must point
to the separately retained private artifact, and the digest must be computed
over that artifact's exact bytes. The reference is recorded in the plan-bound
approval; the evidence content is not copied into the plan. Table names must be
the exact sorted live table inventory. The sole supported exclusion is the
previously reviewed `chirps_analytics_probe_20260908` with its schema digest and
`reviewed_non_account_probe: true`. Do not put credentials, access tokens, DSNs or
provider responses in this file. The CLI refuses group/world-readable files,
symlinks, unsupported fields, stale approvals, project drift, missing flags and
unreviewed table scope.

## Prepare and review

Set `DATABASE_URL` in the process environment from the approved secret source; do
not put it on the command line. Set
`DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA=true` before Python starts. Use
the repository backend virtual environment and invoke:

```sh
cd backend
python -m app.jobs.account_deletion_executor \
  --prepare \
  --request-id <request UUID> \
  --expected-user-id <user UUID> \
  --approval-file /private/c436/approval.json \
  --plan-file /private/c436/plan.json \
  --expected-database <database name> \
  --expected-role <database role>
```

Preparation performs database preflight and reads current provider scope. It lists
owned GCS objects and reads bucket retention settings; BigQuery capture performs
metadata inventory, uncached account-scoped counts and bounded dry runs. These
queries may incur provider charges. It does not delete provider data. A private
plan file is created with `0600` mode and never overwritten. Its digest is printed
as the only plan identifier.

Review the entire private plan and the approval artifact together. Confirm the
account and request UUIDs, exact Stripe customer/account mappings, GCS object names
and generations, bucket policy, BigQuery table/count/estimate scope, retention
codes, database endpoint, role, schema and the recorded manual attestations. The
GCS plan only authorizes deletion of the listed generations; temporary uploads may
arrive later and remain in the deferred cleanup journal. BigQuery's current rows
may be removed while later ingestion is still possible; the durable obligation
continues to disclose and retry that cleanup. Neither is reported as fully drained.

## Execute

After independent review, execute only the same plan and approval file:

```sh
cd backend
python -m app.jobs.account_deletion_executor \
  --execute \
  --request-id <request UUID> \
  --expected-user-id <user UUID> \
  --approval-file /private/c436/approval.json \
  --plan-file /private/c436/plan.json \
  --expected-database <database name> \
  --expected-role <database role> \
  --confirm-digest <exact SHA-256 printed by prepare>
```

Execution rereads the private plan, checks its digest and expiry, compares the
approval file byte-for-byte by canonical content, repeats database identity/ACL,
admission-flag, request, hold and registered-drain checks, and rechecks the exact
Stripe mappings and database-owned media references before constructing delete
providers. Provider adapters then repeat their own current-object, generation,
scope, response, idempotency and readback checks. A mismatch refuses execution.

The executor uses the application's database URL and global session factory, but
each run owns a fresh READ COMMITTED session. It invokes the existing sequential
fulfillment journal, which persists each provider attempt before proceeding and
only tombstones local identity after all required receipts and final admission
checks succeed. Exceptions and unknown outcomes remain blocking evidence; do not
recapture a plan or retry a provider step that preflight reports as ambiguous.

The CLI reports sanitized status and step counts only. A process/database failure
after a transaction commit can leave the operator uncertain whether a transition
completed; inspect the private request preflight again before taking any action.
This tool neither activates the hold nor proves external source drain, SDK thread
termination, or provider-side completion beyond each adapter's exact receipt.

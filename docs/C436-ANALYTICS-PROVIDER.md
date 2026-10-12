# Analytics provider boundary

This is a dormant operator adapter for the reviewed BigQuery Cloud Logging
export tables. It requires an explicitly supplied OAuth token, project,
dataset, immutable user/request snapshot, and verified freeze receipt. It does
not discover credentials, wire itself into the application executor, or make
preview requests automatically. A fresh operator capture uses `capture_preview_snapshot`, inventories the complete dataset page set, binds the dataset location and reviewed physical tables, and records the exact reviewed table schema before an adapter can be constructed. The probe table is an explicit reviewed exclusion; an unknown or newly appearing table blocks the capture.

The target predicate is fixed to the reviewed scalar schema:

    jsonPayload.user_id = @user_id AND jsonPayload.analytics IS TRUE

Table identifiers and nested schema are validated before any DML. The probe
table is excluded unless it is explicitly included in a reviewed scope, and it
still fails schema validation because it has no reviewed user_id field. Unknown
schemas, foreign table references, streaming buffers, snapshot drift, redirects,
oversized responses, malformed jobs, and uncertain transport outcomes fail
closed. A stable job ID is derived from request, snapshot, table, and operation;
an insert conflict resolves only that same job after validating the full query
configuration.

Deletion is sequential and capped. The immutable snapshot must be within the row budget. Every DML request includes the reviewed COUNT as a scalar guard in the same table predicate, so a changed count prevents a delete before rows are affected. Every DML response must contain a strict
numeric affected-row count, followed by a fresh uncached COUNT readback whose
result is exactly zero. BigQuery time travel and fail-safe retention mean this
proves current table rows are removed, not permanent byte erasure; the adapter
does not claim expiry of historical copies or whole-provider deletion. Query results are read from `jobs.getQueryResults`, with job reference, schema, completion, and row count validation; `jobs.get` is used only for job state. Dry-run requests are estimates and are never treated as deletion counts. A fresh uncached verification job is used for each readback. This adapter does not complete the parent email/logs/backups scope.

The freeze and ingestion-drained values are operator evidence supplied to the
adapter, not a mechanism implemented here. Operators must separately establish
write quiescence, reviewed table ownership, provider authorization, and recovery
for an unknown job outcome. No production BigQuery calls or automatic journal
registration are included.

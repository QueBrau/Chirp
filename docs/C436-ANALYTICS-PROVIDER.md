# Account-linked analytics deletion

`account_provider_analytics.py` is a dormant operator adapter. Jose approved
removing a deleting account's identifiable analytics events on October 12.
This code does not discover credentials, register with the application executor,
run on import, or authorize a production query or cleanup.

## Scope and preparation

The fixed predicate is `jsonPayload.user_id = @user_id AND
jsonPayload.analytics IS TRUE`. It uses the exact canonical account UUID and
individually reviewed physical `run_googleapis_com_stdout_YYYYMMDD` tables.
Shared chapter, campus, conversation and event IDs do not establish ownership.
Events without an account ID, including anonymous poll events, are outside this
predicate; the result is not a claim that every possible identifier was erased.

Before capture, an operator must establish and verify account write quiescence
and ingestion drain. `AnalyticsFreezeReceipt` records that external evidence;
it does not implement a freeze or prove a caller's assertions. In-flight exports,
provider credentials, durable request/snapshot storage and recovery are separate
integration work. No new IAM permissions or automatic provider registration
are included here.

The complete dataset inventory must match the reviewed table set. There is one
permitted explicit exclusion: the existing `chirps_analytics_probe_20260908`
probe. `AnalyticsExclusion` requires its reviewed full schema digest and an
operator declaration that it is a non-account probe. Its nested probe schema,
physical table identity and absence of a streaming buffer are also checked.
Schema alone cannot establish that unrelated free-text columns contain no
personal data; the operator must review that exclusion. Unknown tables, views,
schema changes, foreign references and even an empty `streamingBuffer` field
block capture or completion.

## Two stages

1. Call `BigQueryAnalyticsProvider.capture_preview_snapshot` with explicit token,
   project, dataset, request/account UUIDs, verified freeze, sorted table tuple,
   exclusions and a query byte budget. This makes metadata reads, actual uncached
   COUNT queries and non-executing DELETE dry runs. It is not free metadata-only
   inspection: the COUNTs are billable queries and need their own authorization.
   A dry run is only an estimate; only PRECISE or UPPER_BOUND estimates within the
   chosen budget are accepted. Capture inventories metadata again before returning.
2. Review and durably preserve the exact returned snapshot, counts and digest.
   After separate concrete cleanup approval, construct the adapter with
   `reviewed_snapshot_digest` and call `delete_account_data` using the exact
   `c436:<request_uuid>` key. Reuse that same snapshot on an uncertain retry;
   never manufacture a new request to bypass a conflict.

Every real query includes `maximumBytesBilled`; the default is 50,000,000 bytes
per job. The snapshot permits at most 32 total tables, 100,000 reviewed rows and
1,000,000,000 summed per-table query-budget bytes. This sum is not a single
whole-run billing cap: capture uses one COUNT per table, and apply uses a plan
COUNT plus one DELETE and one verification COUNT per target. Retries can incur
fresh verification queries. Every capture/apply call has a 120-second local
deadline, bounded polling, 512 KiB streamed response limits, and no redirects or
environment proxy discovery. A timeout does not cancel a submitted BigQuery job.

Snapshots expire after 15 minutes, and freeze validity is checked repeatedly.
All requests use the fixed Google BigQuery HTTPS origin and US location. Query
parameters contain the account ID; Google job metadata can therefore retain it.
The module does not print requests, tokens or raw provider errors. Snapshot and
receipt storage must be access controlled by the eventual operator integration.

## Deletion and retry evidence

Before any DELETE, a stable read-only plan job binds the request to the snapshot
via its query and configuration label. The plan COUNT's results are deliberately
not reused as fresh evidence. A conflicting plan is rejected. Stable DELETE job
IDs depend on request and table, not a replaceable snapshot. An uncertain insert
or 409 resolves only the same job; every returned/polled reference and execution
configuration must match. Known benign server defaults and narrowly recognized
anonymous SELECT output tables are allowed; other returned configuration blocks.

Each DELETE includes a same-table COUNT guard equal to the reviewed count. A
changed count therefore prevents that table's deletion, and the affected-row
count must equal the preview. This is a per-table operation, not an atomic
transaction spanning the dataset: earlier tables may already be deleted when a
later table blocks. Fresh uncached COUNT jobs verify every target after all
DELETEs. Complete inventory, schemas, retention settings, snapshot freshness and
freeze validity are checked again before confirmation. No saved COUNT result is
accepted as retry verification.

The stable plan is an external BigQuery job binding, not a replacement for a
local durable journal. Operators must preserve the immutable snapshot and job
references and resolve any unknown outcome. The short snapshot lifetime bounds
normal retries well within provider job-history retention; expiry requires
operator investigation and a separately reviewed recovery, not automatic recapture.

## What success means

Success is an `analytics` receipt for current rows in the listed tables only.
It explicitly retains `bigquery_time_travel` and `bigquery_fail_safe`, reports
the verified dataset time-travel hours and seven-day fail-safe period, and names
any excluded probe. It does not claim permanent byte erasure, removal of source
Cloud Logging events, email, database backups or complete account fulfillment.
These new retention codes are not added to the existing four-provider executor;
a later reviewed integration must combine the remaining provider evidence.

All current tests use offline HTTP fixtures. Actual cloud authorization, query
compatibility, complete dataset scope, quiescence and deletion require their
own concrete production preview and approval. Local tests do not prove those.

Contracts: [Job REST resource](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/Job),
[query results](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/jobs/getQueryResults),
[DML semantics](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/dml-syntax),
[retry guidance](https://docs.cloud.google.com/bigquery/docs/reliability-intro),
[time travel and fail-safe](https://docs.cloud.google.com/bigquery/docs/time-travel).

# Analytics delivery and measurement evidence (c373)

Updated: September 8, 2026. Initial audit: September 7 (US Eastern). Owner: Jose/Astra; product metric review: Q.
This updates the c216/c227 delivery assumptions without deleting their historical
completion records. Release of the source fixes remains on c388.

## Findings and proof boundaries

The existing `chirp-analytics-bq` sink selects
`resource.type="cloud_run_revision" AND jsonPayload.analytics=true` and targets
`chirps-prod.chirp_analytics`. Read-only inspection found it enabled, its writer
with project-level BigQuery Data Editor access, and the dataset present. The
metadata-only table listing before the probe returned **zero tables**. Three matching *text*
application-log records were observed over seven days; no user payload was read.
Resource presence did not prove successful delivery.

The actual application stdout handler prefixed every event with
`INFO: app.analytics - ...`. A fresh interpreter using the production logger
could not parse that line as JSON. Cloud Run recognizes structured fields when a
single JSON object is written to stdout/stderr; the prefix prevented that shape
and therefore the sink's JSON predicate. The c373 formatter emits analytics INFO
records as bare JSON while retaining existing formatting for other app logs and
leaving uvicorn access-log scrubbing in place. [Cloud Run logging](https://docs.cloud.google.com/run/docs/logging)

| Evidence | Status / meaning |
| --- | --- |
| Actual local stdout, including configuring logging twice | One parseable event, without a prefix or duplicate handler output |
| Field/value allowlist, logger failure, broken StreamHandler | Local regressions cover content rejection and absence of unsafe stderr diagnostics |
| Profile update with a deferred PostgreSQL constraint failure | No account-change event before a commit that fails; successful retry emits once |
| Sink, writer role, destination | Observed metadata; these alone cannot establish delivery |
| Synthetic event arrival and destination schema | Exact probe/emission IDs and identical payload observed in Logging and `chirps_analytics_probe_20260908`; proves one synthetic Logging API -> sink -> BigQuery delivery, with the limits below |
| Deployed API/WS stdout after source release | Pending c388; a direct Logging API probe cannot prove container collection or the deployed source revision |

No production load, user action, payment, client SDK, retention setting or IAM
change is part of this source fix.

## Event contract and privacy

Every event has `analytics=true`, `schema_version=1`, an allowlisted `event`, a
random UUID `emission_id`, UTC `emitted_at`, and severity INFO. Application data is
restricted to the exact properties below; unknown/missing fields or invalid
values drop the **whole** event. UUID strings are canonicalized, asyncpg UUIDs
work, counts are non-boolean integers from 0 through 2^31-1, and categorical
values come from explicit enums in `backend/app/core/analytics.py`. No arbitrary
`str(object)`, nested metadata or free-form text is accepted; output is capped at
4 KiB. Failure diagnostics are constant text without the event name, exception,
properties or traceback, and a second logging failure cannot escape the emitter.

| Event | Permitted application properties |
| --- | --- |
| user_signed_up | user_id, account_type |
| account_type_changed | user_id, previous_account_type, account_type |
| campus_verification_started | user_id, nullable campus_id |
| campus_verification_redeemed | user_id, campus_id |
| post_created | user_id, nullable chapter_id/campus_id, audience, post_type |
| message_sent | user_id, conversation_id, message_type, recipient_count |
| event_created | user_id, chapter_id, event_id, visibility |
| event_rsvp | user_id, chapter_id, event_id, status |
| poll_voted | poll_id, chapter_id; **no voter identifier** |
| payment_intent_created | chapter_id, cycle_id, user_id, rail |
| payment_succeeded / payment_failed | event_type, cycle_id, user_id, rail |
| pipeline_probe | random probe_id only; **never a product action** |

Anonymous Chirp creation, votes and reports are outside this pipeline. There is
no chirp_id, anonymous-author link, message body, ciphertext, device identifier,
email, token, provider payload, payment amount or provider/customer identifier in
the allowed event schema. The existing router source scan remains a useful
regression tripwire; it is not whole-program proof against indirect calls.

Allowed user/conversation/domain UUIDs are **pseudonymous, not anonymous**. They
can be joined to the application database by someone with both permissions.
Restrict analysis to its purpose and publish aggregates; the schema does not
justify granting broader access. Current dataset ACLs include project
reader/writer/owner special groups, and the sink writer's BigQuery role is broad
at project scope. Actual human/group membership and effective organization-wide
access were not enumerated. No dataset default table expiration was set at
inspection, and time travel was 168 hours. This does **not** establish a 30-day
retention policy or retention of every log bucket/backup. c370/c371 and the
separate IAM work own reviewed access, monitoring and retention changes.

## Payment replay and domain success

Events are best effort **after** the relevant successful domain commit. c373
moves account-type telemetry after commit; its regression uses a real deferred
constraint trigger rather than mocking commit as a harmless no-op. The
application payment ledger remains the source of financial truth.

* Intent creation is emitted only after storing a newly created provider intent.
  A same-rail retrieve/retry does not emit another creation event. This count is
  not the number of HTTP retries or every provider confirmation attempt.
* The c349 webhook receipt and ledger/status transition commit together. An exact
  replay returns without another event; a distinct success notification cannot
  produce another ledger insertion/event for the already recorded payment.
* Failure records an accepted local open-to-failed transition. Repeated failure
  deliveries in failed state do not count more attempts. c387 keeps that same
  reservation held until success or confirmed cancellation. Late failure cannot
  demote a succeeded/canceled reservation. These events cannot count all card
  declines or ACH attempts.
* A process crash after commit but before stdout can lose telemetry. A crash or
  transport replay can also duplicate log delivery. Neither the sink nor the
  emitter creates an atomic domain-event outbox or recovers missing events.
  `emission_id` deduplicates copies of one emission; it is not an exactly-once
  business-event key. Retries of some accepted actions, such as RSVP updates,
  may legitimately have distinct emission IDs. Do not add them as unique people.

## Concrete synthetic delivery probe

The probe is one non-user event from the real local emitter, submitted explicitly
through the Logging API under a dedicated log name. Its `cloud_run_revision`
resource labels name **synthetic-analytics-probe**, not a deployed service. Those
labels satisfy the existing sink filter and are declared synthetic; no Cloud Run
service is created and this is not evidence of a request or process on a real
revision. It tests the Logging API -> existing sink -> BigQuery leg separately
from the local stdout test. Do not label this a deployed-container end-to-end test.

From `backend/`, using the project's Python environment:

```sh
CHIRPS_ANALYTICS_PROBE_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
python -m app.scripts.analytics_probe --probe-id "$CHIRPS_ANALYTICS_PROBE_ID" \
  > /private/tmp/chirps-analytics-probe.json
```

That module only prints one event; it performs no network or database work.
Inspect that the payload has precisely the envelope and `probe_id`, then the
following **explicit write** submits it. It may create one small probe-only
BigQuery table via the existing sink; it does not change configuration or IAM.

```sh
gcloud logging write chirps_analytics_probe \
  "$(cat /private/tmp/chirps-analytics-probe.json)" \
  --project=chirps-prod --payload-type=json --severity=INFO \
  --monitored-resource-type=cloud_run_revision \
  --monitored-resource-labels=project_id=chirps-prod,location=us-central1,service_name=synthetic-analytics-probe,revision_name=synthetic-analytics-probe,configuration_name=synthetic-analytics-probe
```

Use the recorded payload `probe_id` and `emission_id` for exact matching, never a
broad scan of user rows. Read the matching log with an equality predicate and a
small result limit; list dataset table metadata and inspect the actual exported
schema. Query only the observed probe table, with bound string parameters,
`event='pipeline_probe'`, both IDs and `LIMIT 1`. Set a query maximum bytes billed
of 10,000,000 and use a dry run first. Record received timestamp, observed table
and field types; a successful write acknowledgement is not arrival evidence.
Poll at most once per minute for ten minutes, then leave delivery NOT_PROVEN and
inspect metadata-only sink errors/IAM/schema issues. New tables can take several
minutes; existing-table delivery is usually around a minute, not a guarantee.
[gcloud logging write](https://docs.cloud.google.com/sdk/gcloud/reference/logging/write),
[BigQuery log export and schema](https://docs.cloud.google.com/logging/docs/export/bigquery)

The probe-only log/table is excluded from every product query. Repeat container
collection verification after c388 deploys the exact reviewed image to API and
WS; record actual release identity separately. Never generate a real signup,
message, anonymous Chirp or payment merely to fill a dashboard.

## Recorded synthetic result

On September 8 UTC, one local `pipeline_probe` was submitted through the Logging
API under the declared synthetic resource. Logging received it at 03:09:52 UTC.
An exact-table query matching both random IDs returned the identical payload;
that result was recorded at 03:31:37 UTC. Polling was interrupted, so this does
**not** measure export latency or establish delivery within the ten-minute
procedure window. [Sanitized evidence](infra/evidence/c373-2026-09-08.json)
records the actual observation, not an assumed arrival time.

The table is `chirps-prod.chirp_analytics.chirps_analytics_probe_20260908`.
Its `jsonPayload` has `analytics` BOOLEAN, `schema_version` FLOAT, and the other
five envelope/probe fields STRING. This is the **probe table's** schema; product
event tables and deployed API/WS stdout collection remain unverified. One row
returned with `LIMIT 1` proves existence, not uniqueness or complete delivery.

The probe table had no expiration at inspection; this test did not establish or
change retention. The dry run recorded a 10,000,000-byte billing ceiling and a
zero-byte processing estimate, which do not measure actual query charges or
pipeline cost. Only allowlisted evidence is published; raw local query-job
metadata includes operator identity and must remain private. No repeat write is
needed to reproduce the recorded result: query its exact IDs if rechecking it.

## What can be measured honestly

No product rates are published: the verified evidence is a synthetic probe, not
a product-event measurement window. After release, require continuous, observed delivery across the complete measurement
window and an explicit last-arrival watermark. Label unknown/missing delivery as
**unavailable**, not zero. Use UTC half-open intervals, an explicit query end,
late-arrival cutoff (proposed: 24 hours), bounded table/date scans, and dry-run
query bytes before execution. Historical prefixed logs and versionless schemas
cannot silently count as complete cohorts. Exclude pipeline_probe, reject unknown
schema versions, and deduplicate by emission_id before any aggregation.

| Proposed measure | Numerator / denominator and limitation |
| --- | --- |
| Observed daily write actors | Distinct non-null user_id with allowed user-attributed events in one UTC day. This is **not app DAU**: reads, app opens and anonymous activity are not collected. Poll votes cannot contribute actors. |
| Observed D7 write retention | Distinct signup-cohort users with an attributed event in their UTC cohort day +7 / distinct observed signups on the cohort day. Publish only after day +7 and the late-arrival cutoff have completed with coverage. It is not read retention. |
| Verification funnel | Distinct observed users with verification redeemed after an observed start within seven days / distinct observed users starting in the chosen cohort interval; one actor per cohort, complete seven-day follow-up. Unknown campus starts remain in an unknown bucket. It does not measure every email delivery or consented click. |
| Campus activity | Accepted campus-scoped post creations grouped by the event's campus_id and interval, after transport dedupe. Keep null as unknown. Other events lack historical campus attribution; do not infer it by joining a user's current campus. |
| Payment outcomes | Prefer unique paid obligations and amounts from the committed ledger. Analytics can describe observed creation/state transitions by rail, with the retry/failure limits above; it is not a settlement reconciliation report or an all-attempt conversion denominator. |

These are definitions for a reviewed query/report, not new tracking or a shipped
dashboard. Q should validate product wording and cohorts before publishing a
number. Delivery/coverage alerts and bounded retention remain linked follow-ups;
a client SDK, stable attempt IDs or new event dimensions require separate scope.

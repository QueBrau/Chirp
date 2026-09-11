# Launch monitoring: preparation, not operational acceptance

c370 remains **OPEN**. This slice supplies a read-only inventory checker, two
runtime failure signals and a post-fallback observation. It does not install
policies, notification channels, checks, schedulers or metrics, and it does not
verify alert delivery. A deployment is required before the new runtime signals
can exist in production. No live inventory or recipient information belongs in
this public repository.

## Collect configuration evidence privately

Run with an already authorized read-only identity; do not grant additional roles
merely to make this check green. Required collection permissions are
`monitoring.alertPolicies.list`, `monitoring.uptimeCheckConfigs.list`,
`monitoring.notificationChannels.list`, and `monitoring.metricDescriptors.list`.

```sh
monitoring_dir=$(mktemp -d)
chmod 700 "$monitoring_dir"
umask 077
scripts/monitoring-check --project YOUR_PROJECT --gcloud /path/to/gcloud \
  > "$monitoring_dir/inventory.json"
```

If the local Python installation lacks its trusted roots, supply `--ca-file`
with an installed, trusted CA bundle, or repair that installation. TLS and host
verification remain enabled; redirects are refused so authorization is not
forwarded to another host. The token stays in process memory and is never a
command-line argument, report field or diagnostic. The tool has no write/API
activation mode and does not retrieve recipient labels, policy conditions,
documents, HTTP authentication headers or message content.

Exit 0 means the inspected inventory is complete without the checker's basic
configuration gaps; 1 means an empty/disabled collection or a missing/changed
native descriptor needs review; 2 means access, transport, schema or pagination
prevented a complete inventory. **No exit code establishes operational coverage.**
Every report keeps delivery, responders, external coverage and time-series
coverage unverified. A descriptor can be available with no data for our service.
The checker cannot determine whether existing policies match the needed signals.

Each of six collection queries has at most ten pages of 100 records and a 1 MiB
response cap per page. The default 15-second timeout applies to the credential
command and individual blocking network operations, not a whole-run deadline.
Denied, malformed, cyclic or truncated collections are errors, never evidence
that monitoring is absent. SDK subprocess diagnostics and upstream error bodies
are not printed. Keep even the aggregate report private.

These are project-scoped APIs. The uptime list includes valid configurations;
an empty result alone does not inventory invalid configurations. Ask the project
owners to reconcile organization-owned metrics scopes, external uptime tools and
other incident systems in the private operations record. Record who checked,
which scope they could inspect, and when; an inaccessible scope is unknown.
See the [alert-policy list](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies/list),
[uptime list](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.uptimeCheckConfigs/list),
[channel list](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.notificationChannels/list)
and [metrics-scope API](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v1/locations.global.metricsScopes/get).

## Runtime observations

The exact logger is `app.operational`; its stdout records are bare JSON so Cloud
Logging can select `jsonPayload`. c373 analytics formatting remains separate.
Each record has only `schema_version=1`, `signal_family="chirp_operational"`,
the fixed `event`, `severity`, `observation_scope="process"`, and `sampled=true`.
There are no dynamic labels, identifiers, paths, keys or exception bodies.

| Event | Evidence and limit per process |
| --- | --- |
| `sql_pool_capacity_503` | The existing SQLAlchemy checkout-timeout handler is returning 503 with `Retry-After: 5`. At most one warning per 60 seconds. Successful requests, auth rejections and arbitrary 503s do not emit this signal. |
| `rate_limit_fallback` | A limiter operation failed to use Redis and takes its existing local fallback. At most one warning per 600 seconds, in addition to the existing c292 prose warning. |
| `rate_limit_redis_success_after_fallback` | An operation **begun after the latest observed fallback** completed Redis work. At most one INFO record per 600 seconds. A valid Redis denial still counts as a successful Redis operation. |

Failure epochs prevent an older in-flight success from reporting a post-fallback
success after a newer failure. A pending call can still fail later, and a
different instance can still be degraded. This INFO event is not fleet recovery
and must not automatically resolve a fleet incident. The throttle can suppress
repeated transitions. Persistent failures produce another warning only when a
failing request arrives after the interval; no traffic means no new observation.
Silence is not health, and these records are not exact failure counters.

There are three fixed throttle slots, no per-user monitoring state and no new
background task. In a stable process the new logger permits at most 60 capacity,
6 fallback and 6 post-fallback records per hour. Restarts reset that budget.
Existing logs and cloud request logs are additional volume. The new operational
emitter and the existing c292 limiter warning isolate logging failures from the
response/local budget and suppress failing-sink diagnostics. This does not change
unrelated application loggers. Missing log delivery remains an independent
operational failure to detect, not an excuse to report health.

Proposed Logs Explorer selectors, to validate after deployment:

```text
resource.type="cloud_run_revision"
jsonPayload.signal_family="chirp_operational"
jsonPayload.schema_version=1
jsonPayload.event="sql_pool_capacity_503"
```

Use the two deployed service names as fixed resource filters. For the limiter,
substitute `rate_limit_fallback`. A log-based alert on either warning is a useful
first notification, with a reviewed 15-minute notification throttle and an
explicit incident follow-up. It is not a request-error-rate calculation. Review
the platform's missing-data and auto-close settings: an automatically closed
incident does not prove recovery. [Alert behavior](https://docs.cloud.google.com/monitoring/alerts/concepts-indepth)
describes evaluation windows, data gaps and closure rules.

## Current-service signal plan

Start with native metrics and these bounded logs. No exporter, tracing backend
or per-request custom log stream is introduced for the approximately $125/month
launch budget. Instrumentation and any synthetic checks still need an explicit
cost allowance in the spend plan. Thresholds below are **proposals**, not
measured service objectives or activated policies.

For Cloud Run queries, select `resource.type="cloud_run_revision"`, the project,
region and `resource.labels.service_name`. Keep API and WS separate and combine
all serving revisions deliberately. Built-in CPU, memory, instance, request and
job metrics are documented in [Cloud Run monitoring](https://docs.cloud.google.com/run/docs/monitoring).

| Signal | Source and proposed activation rule | First response |
| --- | --- | --- |
| API and WS process availability | Separate HTTPS `/_health` uptime checks, proposed 60-second cadence with multiple checker locations and a sustained failure rule. This endpoint proves only process responsiveness. | Check ingress, latest revision and startup failures; then inspect dependency signals. Do not infer SQL, Redis or authenticated WS success. |
| API request failures | `run.googleapis.com/request_count`; compare 5xx counts with all requests over the same 5-minute window. Proposed page at >5% with >=20 total requests for 5 minutes; retain uptime checks for low/zero-traffic outages. | Correlate revision, SQL capacity, provider failures and rate-limiter fallback. Classify 429/401/403 separately; never hide their existence by calling them success. |
| API latency | `run.googleapis.com/request_latencies`, distribution in milliseconds. Proposed service-level investigation at p95 >1000 ms for 10 minutes. | Inspect dependency waits, hot routes and instance pressure. This aggregate cannot establish separate read/write targets. |
| WS stream health | c354 explicit stream close reasons and bounded local socket/Redis harness in [WEBSOCKET-RESOURCE-LIMITS.md](WEBSOCKET-RESOURCE-LIMITS.md). An authenticated ready/catch-up/receive synthetic remains to be designed and authorized. | Separate expected disconnect/revocation from broker/backpressure failures. Check Redis and client recovery. No dummy message or auth credential belongs in an uptime URL. |
| Instance pressure | Run `container/instance_count`, `container/cpu/utilizations`, `container/memory/utilizations`. Review sustained p95 memory/CPU >80% for 10 minutes and proximity to the reviewed instance ceiling. | Compare deployment/pool envelope before increasing instances. See [DEPLOY.md](DEPLOY.md). Autoscaling blindly can exhaust SQL connections. |
| SQL availability and capacity | Cloud SQL `database/up`, `database/postgresql/num_backends`, `database/cpu/utilization`, `database/disk/utilization`, `database/disk/bytes_used`; plus the new pool-503 signal. Proposed SQL-up failure page, disk >80% sustained warning and >90% escalation, connections approaching the deployment's reserved ceiling. | Inspect connections, active transactions and storage growth. Apply the connection reserve policy, not a guessed universal count. Pool wait-time distribution remains uninstrumented. |
| Redis pressure and fallback | Memorystore `clients/connected`, `server/uptime`, `stats/memory/usage_ratio`, `stats/evicted_keys`; new limiter warning and existing startup/WS broker errors. Proposed memory >80% for 10 minutes and unexpected eviction warning. | Check connectivity/configuration before resizing. Uptime and memory do not prove pub/sub delivery; direct reachability and reconnect signals still need operational verification. |
| Job failure and missed schedule | Run `job/completed_execution_count` by configured job/result, and Scheduler execution start/end logs. Record each actual schedule, timezone, allowed duration and retries. Notify on a failed final execution or no expected successful completion by that schedule plus its reviewed grace period. | Check invocation identity, secret access, exit status and domain report. Scheduler accepting a Run execution is not completion of that execution. |
| Purge backlog and blocked work | Existing purge aggregate JSON: `mode`, `status`, `remaining`, `capped_counts`, `batches_committed`, `commit_outcome_unknown`. Successful dry-run/preview is not successful deletion. | Follow the purge section of [RECOVERY-RETENTION.md](RECOVERY-RETENTION.md). Investigate failed, timed-out or blocked apply; do not rerun unbounded deletes or infer remaining=0 from unknown. |
| Backup freshness and PITR | Scheduled read-only `scripts/recovery-check` report, proposed automated-backup start age <=36 hours and latest recovery lag <=15 minutes. Alert on its gap/error and on a missing report after its own schedule plus grace. | Follow [RECOVERY-RETENTION.md](RECOVERY-RETENTION.md). Daily backup success does not prove PITR or a restore. Job wiring and actual recovery rehearsal remain open. |

Metric prefixes in the shorter table cells are `run.googleapis.com/`,
`cloudsql.googleapis.com/` and `redis.googleapis.com/`. Use `cloud_run_job` for
job metrics, `cloudsql_database` for the selected SQL metrics, and `redis_instance`
for the selected Memorystore metrics; do not substitute Redis Cluster or managed
SQL connection-pool metrics for this architecture. Definitions are in the
[SQL metric catalog](https://docs.cloud.google.com/monitoring/api/metrics_gcp_c#cloudsql)
and [Run/Redis metric catalog](https://docs.cloud.google.com/monitoring/api/metrics_gcp_p_z).
Scheduler publishes [execution start/end logs](https://docs.cloud.google.com/scheduler/docs/viewing-logs);
the collector intentionally does not invent a Scheduler metric family.

The Run request counter excludes requests rejected before reaching an instance
and is recorded at request completion. Its `route` label is always empty; there
is no HTTP-method label in these selected descriptors. Request latency excludes
container startup. A WS request duration measures connection lifetime, not a
message's delivery delay. Native request metrics are sampled at 60 seconds and
can arrive another 120 seconds later; alert expectations must account for that.
These limits come from the same Run metric catalog, not a production capacity test.

For count/rate comparisons, align DELTA counts with `ALIGN_SUM` over the same
300-second windows, then `REDUCE_SUM` across revisions while retaining service.
Apply identical grouping/windowing to numerator and denominator. A zero or
missing denominator is unknown, not a perfect success rate. For service p95,
use distribution-preserving `ALIGN_SUM` followed by `REDUCE_PERCENTILE_95` across
the aligned distributions; do not average individual revision percentiles.
For gauges, use a reviewed `ALIGN_MAX`/`ALIGN_MEAN`; sum disjoint PostgreSQL
database connection counts, and use worst-resource values for utilization so
averages do not hide a saturated resource. For CPU/memory distributions, use
percentile aggregation rather than treating the distribution as a scalar.
These operators are documented in the [aggregation API](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies#Aggregation).
Validate selected time series and units in a read-only query before activating
each policy; descriptor checks alone do not validate these alert expressions.

## Targets and operational hand-off still required

The proposed alpha targets remain: core-operation success >=99.5% over a rolling
30 days, server read p95 <=500 ms, ordinary durable-write p95 <=750 ms, online
delivery p95 <=2 seconds, and zero expected-load pool timeouts. Before measurement,
write the exact core-operation set, eligible responses, expected auth/validation
failures, maintenance rules, and latency boundaries. Never silently drop 5xx,
pool failures, provider uncertainty or low-traffic windows from the denominator.
Separate read/write timing and online receipt/queue-age instrumentation remain
open; server send completion cannot prove receipt. Security or payment invariant
violations remain release-blocking regardless of averages. c356's future durable
outbox/push must add its own signals when implemented, but is not a prerequisite
to activating today's service, database, Redis, job and recovery alerts.

In the private operations record, each activated signal needs a named primary
and backup who have accepted ownership, their verified channels, the runbook,
the exact policy/window/threshold and the last delivery test. Names and contact
details are intentionally absent here. The initial escalation proposal is
primary acknowledgement within 5 minutes for a page, backup at 10 minutes, then
the agreed incident lead; the responders must accept or adjust it. Warnings need
a named next-business-day owner. No acknowledgement or escalation has been tested
by this change.

With explicit authorization, stage a synthetic notification test without
failing production dependencies or sending member messages. Confirm receipt,
acknowledgement, backup escalation, and resolution behavior; preserve timestamps
privately. Verify a normal signal and missing-signal scenario for every intended
policy, including purge completion, missed schedules and stale recovery reports.
Any evidence producer that is not scheduled, deployed or observed is still a
coverage gap. Mark c370 done only after the current-service coverage matrix and
responder/delivery evidence are accepted. The code and fixture tests alone are
preparation, even when CI passes.

## Applying the policies

This slice ships policy-only, native-metric alert policies: the eleven files
under `infra/monitoring/policies/`, covering API request failures, API
latency, Cloud Run instance CPU/memory pressure, SQL availability, SQL disk
warning/critical, SQL connections approaching the reserved ceiling, Redis
memory pressure, Redis unexpected eviction, and failed-execution alerts for
both `chirp-purge` and `chirp-media-reconcile`. `{{PROJECT}}` and
`{{NOTIFICATION_CHANNEL}}` are the only templating points in any policy body;
`scripts/monitoring_apply.py` substitutes `{{PROJECT}}` unconditionally and
`{{NOTIFICATION_CHANNEL}}` only under `--apply`. No policy body contains a
literal project number or channel id.

Dry-run first, which makes zero write calls and only prints the plan (create,
update or no-op per policy `displayName`):

```sh
scripts/monitoring-apply --project YOUR_PROJECT --gcloud /path/to/gcloud
```

Create the notification channel in the console first (this tool never creates
channels), then apply with its resource name:

```sh
scripts/monitoring-apply --project YOUR_PROJECT --gcloud /path/to/gcloud \
  --apply --channel projects/YOUR_PROJECT/notificationChannels/CHANNEL_ID
```

`--apply` without `--channel` exits 2 before any network call. Existing
remote policies are matched to local files by `displayName`; a matching
policy already identical to the local body (ignoring server-assigned fields
and, when a channel is supplied, the channel itself) is a no-op, so re-running
`--apply` against unchanged files makes no write calls at all. The tool never
deletes a policy. Auth reuses `scripts/monitoring-check`'s `gcloud auth
print-access-token` closure and verified-TLS, no-redirect opener, extended
with POST and PATCH against the same `monitoring.googleapis.com/v3` host; the
bearer token never enters argv, logs or the report. Required write
permissions, parallel to the read permissions listed above, are
`monitoring.alertPolicies.create` and `monitoring.alertPolicies.update`.

Field names (`conditionThreshold`, `comparison`, `thresholdValue`, `duration`,
`trigger`, `aggregations` with `alignmentPeriod`/`perSeriesAligner`/
`crossSeriesReducer`/`groupByFields`, `denominatorFilter`,
`denominatorAggregations`, `combiner`, `alertStrategy`) were checked
against the [AlertPolicy REST v3 reference](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies)
and the [Aggregation reference](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.alertPolicies#Aggregation)
at build time; `scripts/tests/test_monitoring_apply.py` round-trips every
policy through a strict validator that fails on any key outside that
allowlist, since a rejected body is a failure mode a fixture-only test cannot
otherwise see. Every `metric.type` a policy filters on must be a member of
the frozen native-metric inventory copied into
`infra/monitoring/evidence/c370-inventory-2026-09-08.json`; a policy
referencing anything else is skipped (exit code 1) rather than applied.
Several thresholds not stated explicitly in the signal-plan table above (the
SQL-up duration, the disk-warning/critical durations, the connections
ceiling's absolute number, the Redis eviction window, and the job-failure
evaluation window) are judgment calls, marked in each policy's own
`documentation.content` as initial and due for review after 7 days once real
traffic exists. The API request-failure-rate policy alerts directly on the
runbook's 5xx-count/total-count ratio using `MetricThreshold`'s native
`denominatorFilter`/`denominatorAggregations` fields (no query-language
condition type is needed for a ratio rule); the runbook's proposed total
>= 20 requests floor is not separately enforced by this condition and is
documented as a known limitation in the policy's own `documentation.content`.

`scripts/monitoring-apply` sources the shared `scripts/lib/pick-python.sh`
helper (c392, merged) via `chirp_pick_python`, the same as
`scripts/monitoring-check` and every other wrapper listed in
`backend/tests/test_c392_script_interpreters.py`'s `WRAPPERS`.

**Out of scope for this slice, deliberately** (tracked as the c370 follow-up):
uptime checks for the two `/_health` endpoints and the alert policies that
reference their check id; the three log-based metrics for
`sql_pool_capacity_503`, `rate_limit_fallback` and the purge job's stdout
aggregate; the alert policies built on those log-based metrics; and the
missed-schedule half of the job-failure signal (`conditionAbsent` against a
Cloud Scheduler cadence). On that last point: no Cloud Scheduler cron,
timezone or grace period for `chirp-purge` or `chirp-media-reconcile` is
checked into this repository (repo-wide grep for `schedule`/`cron`/
`Scheduler` in `infra/*.json` and beyond finds nothing outside this
runbook's own prose and `board.html`), so a `conditionAbsent` window cannot
be built without fabricating a cadence. The follow-up slice needs a
checked-in schedule for both jobs before this half can be built.
`rate_limit_redis_success_after_fallback` intentionally gets nothing in
either slice - the signal-plan table above already states it "must not
automatically resolve a fleet incident", so it is not alert-worthy.

## Local verification

`backend/tests/test_c370_operational_signals.py` exercises actual local SQL pool
exhaustion, fallback/success ordering, cancellation, fixed log budgets, bare JSON
stdout and broken sinks. `backend/tests/test_c370_monitoring_collector.py` exposes
the standard-library collector fixtures to normal backend CI; the same cases
also run with `python3 -m unittest scripts/tests/test_monitoring_check.py`.
They test pagination, permission denial, empty/invalid metadata, descriptor drift,
TLS/redirect handling and diagnostic redaction without cloud credentials.
`backend/tests/test_c370_monitoring_apply_collector.py` exposes
`scripts/tests/test_monitoring_apply.py` the same way, covering dry-run
zero-write-calls, the `--apply` without `--channel` guard, create-versus-update
matching by `displayName`, a second `--apply` landing all no-ops against a
stateful fake API, the required-shape and inventory-membership check (with a
deliberately bad fixture proving it is discriminating), the strict REST-shape
round-trip that fails on any key outside the AlertPolicy allowlist, and the
scan for a hardcoded project number or channel id outside the two templating
points.

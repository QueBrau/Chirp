# Network and Redis security preparation (c372)

This change prepares a narrower network and authenticated, encrypted Redis path.
It does **not** change cloud resources, Redis runtime settings, IAM, service sizing,
or the shared API/WS architecture. c372 remains open until staged positive and
negative access tests, cold starts, broker recovery and a reviewed rollout succeed.

## Repeatable evidence

[infra/deployment.json](infra/deployment.json) remains the source for project,
region, API/WS names, runtime identity, connector and Cloud SQL attachment.
[infra/network-review.json](infra/network-review.json) adds only inventory targets,
existing SQL/bucket controls to preserve and **proposed** Redis controls. It does
not promote today's Redis settings into desired state.

Run from the repository root with existing logged-in gcloud metadata permissions:

```bash
scripts/network-audit --gcloud "$HOME/google-cloud-sdk/bin/gcloud" --report /tmp/chirp-network-review.json
```

The collector runs projected `describe`, `list`, `get-effective-firewalls` and
`get-iam-policy` calls with an explicit project. No Redis command, secret payload,
credential file, database session, policy modification or reachability probe runs.
Provider stderr/bodies, private addresses, personal principals, conditional IAM
expressions and unknown role names are never serialized. Published summaries
retain counts, known runtime membership, approved roles and condition presence.
Errors use fixed labels. Each provider subprocess has a 30-second timeout; this
multi-resource inventory can take several minutes when reads time out.

Exit **2 / `NOT_PROVEN` is intentional**, even when all requested metadata reads
succeed. `metadata_complete_for_requested_scopes` means those projections were
collected, not that every setting is known or permissions are effective. Omitted
Redis `authEnabled` stays `null`; it is not evidence of AUTH enabled. An unavailable
or malformed scope is listed explicitly while independent reads continue.
For classic TCP/UDP rules, an empty port list means all ports, not none; it is
kept in the summary as the API reports it. [Firewall field semantics](https://docs.cloud.google.com/compute/docs/reference/rest/v1/firewalls)

The [September 8 sanitized observation](infra/evidence/c372-network-2026-09-08.json)
covers **16:22:45–16:23:54 UTC**:

- Redis: ready, Basic, direct peering to the review network, TLS **disabled**,
  port 6379, AUTH enabled status not supplied by the metadata response.
- Connector: ready, current deployment-source attachment matches API/WS templates
  **and their serving revisions**. Both use private-ranges-only egress. Both jobs
  use identities different from the shared API/WS identity and have SQL attachments;
  neither reports a match to the reviewed connector.
- Four visible classic rules include unscoped SSH/RDP and broad internal ingress.
  Effective-firewall enumeration additionally exposes six rules with connector-style
  names. The collector's `connector_name_pattern` label is a naming hint, not an
  authoritative management-ownership attestation.
  The ordinary project VM list is empty. This does **not** establish a publicly
  exposed VM or Redis instance.
- SQL: public IPv4 configured, zero authorized-network entries reported,
  `ENCRYPTED_ONLY`. Media bucket: public access prevention enforced and uniform
  bucket-level access enabled.
- Direct policies for the seven declared secrets contain an unconditional shared
  runtime secret-accessor binding. The bucket includes conditional grants; project
  and other role summaries also require interpretation. Neither group membership,
  IAM inheritance/deny/principal access boundaries nor conditions were evaluated.

The separate [approved numeric SQL observation](infra/evidence/c362-sql-2026-09-08.json)
and [c362 follow-up](DEPLOY-CONFIGURATION.md#approved-sql-follow-up--september-8-2026-160055-utc)
do not prove a current full release/configuration match.

## Consumers and paths

This is the source-grounded required-consumer matrix plus observed attachments.
Credentials and the live `DATABASE_URL`/`REDIS_URL` payloads were not inspected by
the network collector. Verify the actual data path in staging before restricting it.

| Consumer | Required resource/path | Identity and boundary | Evidence and open check |
| --- | --- | --- | --- |
| Mobile/browser | HTTPS API; WSS gateway | Firebase/backend authorization and membership checks; Cloud Run invoker is public | Public invoker is intentional HTTP reachability, not authorization to data. Compiled client routing is c362/c361 acceptance. |
| API | Redis counters and event publishing through connector → private Redis endpoint | Current shared runtime identity; proposed Redis AUTH over TLS | `ws/pubsub.py`, `services/rate_limit.py`; attachment observed, credentials/packet path untested. |
| WS | Redis subscribe/publish through the same connector | Same runtime identity, currently per-socket subscription | `ws/gateway.py`, `ws/pubsub.py`; staged subscribe/reconnect/deny proof required. c354 owns bounds and cleanup. |
| API + WS startup | Throwaway Redis PING | Same URL configuration, separate temporary client | `main.py:_probe_redis`; failure is logged and does not prevent HTTP startup. A healthy HTTP endpoint is insufficient Redis proof. |
| API + WS | Cloud SQL via configured attachment/selected client path | Cloud SQL IAM plus database role | Attachment observed; route, DB authorization and encrypted transport must be checked independently. Keep c362 aggregate pool budget. |
| Purge job | Cloud SQL only for purge data work | Its job identity and restricted DB role, not presumed to be shared runtime | SQL attachment observed; secret/role acceptance belongs with c369. No Redis need shown by `jobs/purge.py`. |
| Media reconcile job | Cloud SQL and GCS | Its job identity, scoped media/database access | SQL attachment observed; `jobs/media_reconcile.py` shows no Redis requirement. Preserve c355 deletion/lease boundaries. |
| API media handling | GCS/Google APIs over HTTPS; capability URLs for authorized reads | Runtime storage/IAM signing grants and application checks | Private bucket controls observed; direct conditional policies do not prove effective permission. |
| API outbound integrations | HTTPS Firebase/Google, Stripe and configured email provider | Existing provider credentials and service grants | Preserve current public egress. Q's SES/c240 and c375 identity separation remain separate lanes. |

Cloud SQL Auth Proxy uses Google API access and a separate encrypted instance
connection. Its network path is not equivalent to a direct PostgreSQL connection;
confirm the chosen deployment mechanism before writing port rules.
[Cloud SQL Auth Proxy networking](https://docs.cloud.google.com/sql/docs/postgres/sql-proxy).

## Proposed network and IAM restrictions

1. Inventory actual required destination ranges/ports privately for each consumer
   above, including DNS, Google APIs, SQL and connector health infrastructure.
   Retain the public, allowlisted summary here; attach staged verdicts by path label.
   Do not apply a VM-tag-only ingress rule and assume it protects the managed Redis
   producer endpoint. Validate connector egress and the producer/peering behavior.
2. Preserve connector-managed priority-100 infrastructure rules. Design custom
   restrictions with priorities greater than 100 numerically, ordered ahead of
   applicable broad rules, and scoped to the chosen connector/resources. Hidden
   managed rules must be reviewed alongside classic/hierarchical policy behavior.
   The effective CLI's flattened rows omit protocol details; the collector marks
   that explicitly. Regional and producer policy coverage remains incomplete.
   [Connector firewall rules](https://docs.cloud.google.com/vpc/docs/serverless-vpc-access).
3. Retire broad default SSH/RDP/internal access only after a fresh consumer inventory
   and allow/deny proof. The empty ordinary VM list is useful context, not proof that
   a rule is safe to delete. Do not alter Direct VPC attachment here; c376 owns that
   measured experiment and requires a revised path matrix if selected.
4. For each runtime/job identity, review project **and resource** policies, custom
   roles, service-account impersonation, inherited grants, conditions and deny/PAB
   effects. Preserve narrowly scoped secret/storage grants. Public Run invoker does
   not justify a public bucket/secret grant. c375 owns separate API/WS identities;
   this preparation introduces none and does not claim least privilege achieved.

## Redis AUTH/TLS proposal

The existing instance cannot be upgraded to TLS in place: Memorystore for Redis
enables TLS only at creation, and that setting cannot later be disabled. The
proposal is a new TLS instance, port **6378**, with AUTH and a trusted CA bundle.
This is Memorystore for Redis, not the separate Redis Cluster product.
[TLS setup and CA installation](https://docs.cloud.google.com/memorystore/docs/redis/manage-in-transit-encryption).

The locked **redis-py 8.1.0** client already accepts `rediss://` with percent-encoded
passwords and `ssl_ca_certs` through the existing `Redis.from_url` calls. Proposed
Secret Manager URL shape (placeholders only):

```text
rediss://:PERCENT_ENCODED_AUTH@TLS_ENDPOINT:6378/0?ssl_ca_certs=%2Fvar%2Frun%2Fredis-ca%2Fserver-ca.pem&ssl_cert_reqs=required&ssl_check_hostname=true
```

Mount the instance's public CA bundle read-only at that path on **both** services,
including cold-start revisions. Publish and pin the reviewed credential/version
and CA deployment change together with the c362 source update. Do not put AUTH in
source, shell history, deployment command output or logs. URL-encode the password
and query values with a library, not hand substitution. Existing singleton and
startup clients both need the same trust settings. No runtime patch or CA mount is
included here. [redis-py asyncio connection implementation](https://redis.readthedocs.io/en/stable/_modules/redis/asyncio/connection.html).

Keep certificate verification required. Test the new Memorystore endpoint's actual
certificate identity against the chosen IP/name; our synthetic SAN test does not
establish Google's deployed certificate compatibility. A mismatch must stop the
rollout for investigation, not silently set verification to `none` or disable
hostname checking. CA updates must include all currently applicable certificates;
the client SSL context/pool must be recreated to load a changed bundle. Rehearse
that new revision before old trust expires.

AUTH can be enabled on an existing instance, but alone it does not encrypt the
credential in transit. The documented reset disables and re-enables AUTH, creating
a new credential and interrupting authenticated connections; it is not a seamless
dual-key rotation. Prefer the reviewed TLS replacement, and plan later rotations
as explicit reconnect/credential coordination events.
[AUTH behavior and reset](https://docs.cloud.google.com/memorystore/docs/redis/manage-redis-auth).

## Staging, cutover and rollback acceptance (not executed)

1. Choose the new instance configuration, named owners, short test window and
   cleanup deadline. Budget temporary overlapping brokers and verify their memory/
   client limits. Preserve current service sizing; HA and cost decisions remain
   c368/c287/c371. No resource is created or removed by this collector.
2. From both staged API and WS cold starts, prove TLS PING, authenticated publishing,
   subscribe/delivery, shared rate limiting across instances and recovery after a
   broker restart/connection drop. Capture fixed outcome/count/latency labels only.
   Preserve SQL encrypted-only and bucket privacy with positive and negative checks.
3. Test unauthorized network origin, unauthorized identity/secret access, plaintext,
   missing/wrong AUTH, untrusted CA, mismatched certificate identity and stale CA.
   Record which layer rejects each attempt. A timeout or a failed metadata read
   does not by itself prove a firewall denied a packet or IAM denied an operation.
4. Plan a coordinated broker cutover. API publishers on the new broker cannot reach
   WS subscribers still on the old broker. A routine serialized API-then-WS rollout
   can therefore lose realtime notifications and split rate-limit counters. Rehearse
   a short maintenance/drain strategy with accepted-write handling and client
   resynchronization, or separately review a bridge; do not invent durable delivery.
   c356/c345 own durable event work. Keep all old/new revisions inside c362's pool
   envelope and verify old sockets have drained before declaring convergence.
5. Preserve the prior broker and exact URL/CA/revision references for a bounded
   rollback window. Reverting requires coordinating **both** consumers again;
   it cannot replay missed ephemeral pub/sub messages or restore old counters.
   During Redis outage the current rate limiter falls back per process, so its
   limits are no longer global. c354/c346 own bounded socket degradation/reconnect.
6. Require reviewed source, CI, same-release deployment checks, device reconnect
   checks and staged allow/deny evidence before a production operation. Record
   operator approval/window and outcome. Remove the old broker only after rollback
   expiry and a separate reviewed cleanup; never delete it to make a test pass.

## Executed local compatibility checks

The opt-in [TLS fixture](scripts/tests/test_c372_redis_tls.py) starts only its own
loopback TLS Redis with generated CA/server certificates and synthetic AUTH. It
does not accept a Redis endpoint argument or production credential. Missing binary/
OpenSSL prerequisites fail explicitly. Tests exercise actual redis-py and the app's
factory/startup probe; they are separate from the default backend CI suite because
they require a TLS-enabled Redis binary. Run from the repository root:

```bash
CHIRP_TEST_REDIS_BINARY=/path/to/verified/redis-server CHIRP_TEST_OPENSSL=/path/to/openssl backend/.venv/bin/python -m pytest -q scripts/tests/test_c372_redis_tls.py
```

On the Intel Mac, use the documented `sys.modules["readline"] = None` pytest launch
workaround if needed. Redis's child locale is explicitly `C`; unsupported inherited
`C.UTF-8` otherwise prevents this macOS binary from binding. Tests disable persistence,
clean up their own PID, and remove synthetic keys/configuration after use.

The local fixture uses verified official Redis **7.2.15** source, TLS build enabled,
with SHA256 `7bf7975331511fdb788e85dae63964b128fccee1df026a10db57444babc9c9c4`
([official release hashes](https://github.com/redis/redis-hashes)). This differs from
the observed managed Redis 7.0 service and does not replace staging against its
real certificate, version and network.

The first local run exposed a real operational distinction: a broker restart can
make the next pooled operation fail. An explicit retry of idempotent PING then
reconnects with the trusted rotated certificate. These tests do not promise automatic
replay of arbitrary Redis commands. The metadata regression suite is
[test_c372_network_evidence.py](backend/tests/test_c372_network_evidence.py); it runs
without cloud/Redis/DB access in normal backend CI.

September 8 validation: **34 metadata cases** and **12 actual TLS/AUTH cases**
passed, zero skips. The TLS suite retained redis-py 8.1.0's default RESP3 protocol.
A combined earlier run also passed the 67 current c362 regression cases. A
disposable mutation disabling peer/hostname verification was rejected by the
untrusted-CA, wrong-CA and wrong-hostname cases; source stayed unchanged. These
are local compatibility results, not staged or production acceptance.

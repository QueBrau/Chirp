# Authenticated deployment verification (c361)

`scripts/deploy-verify` is a read-only release check. A successful unauthenticated
run says `ROUTING_ONLY`; only a successful authenticated run says
`AUTHENTICATED_READY`. HTTP 200/401 routing probes alone cannot establish readiness.
The script does not deploy, migrate, send messages/email, or create payments.

The wrapper picks its interpreter in this order: `$CHIRP_PYTHON`, then the repo's
`backend/.venv/bin/python`, then `python3` on `PATH`, and refuses to start (exit 2)
if the winner holds no CA certificates - a python.org framework build without its
Install Certificates step fails every https probe at the TLS layer and would
otherwise report a false `NOT_READY` (c391, deploy windows #13 and #14). The chosen
interpreter is printed to stderr so a report can cite it.

## Release inputs

Use the expected schema revision and immutable image digests from the reviewed
release/build record, and the API/WS revision names from that deployment. Do not
replace an expected value with whatever is currently serving merely to pass.
Both services must have converged to their expected revision. Split traffic,
unaccounted tagged revisions, missing metadata and in-progress readiness fail.

Load a short-lived Firebase ID token for the known QA account into the exported
`DEPLOY_VERIFY_BEARER` environment variable through your approved credential
source. Do not put the value in shell arguments, a pasted command, a report or a
tracked file. The script rejects the former command-line bearer option, redacts
argument errors by omitting their values, and removes the bearer environment
variable from `gcloud` child processes. Avoid shell tracing while handling it.
Setting the bearer also selects authenticated mode automatically; incomplete
release inputs then fail instead of silently falling back to routing checks.

The fixture user must exist, be unsuspended, and have access to the specified
real campus. The `/auth/me` response must match both UUIDs. Nil UUIDs and arbitrary
path fragments are rejected. The fixture need not be a platform administrator.

With those non-secret shell variables set and the bearer already exported:

```sh
scripts/deploy-verify --authenticated \
  --project "$PROJECT" --region us-central1 \
  --base-url "$API_URL" --ws-base-url "$WS_URL" \
  --user-id "$QA_USER_ID" --campus-id "$QA_CAMPUS_ID" \
  --expected-schema "$RELEASE_SCHEMA_HEAD" \
  --api-revision "$API_REVISION" --ws-revision "$WS_REVISION" \
  --api-image-digest "$API_IMAGE_DIGEST" --ws-image-digest "$WS_IMAGE_DIGEST" \
  --report /tmp/chirp-deployment-verification.json
unset DEPLOY_VERIFY_BEARER
```

Digest arguments use `sha256:` followed by 64 lowercase hexadecimal characters.
`--project` or explicit `DEPLOY_VERIFY_PROJECT` is required; the CLI does not use
gcloud's implicit project. Services default to `chirp-api` and `chirp-ws`; override
with `--api-service`/`--ws-service` only when verifying another deployment. The
metadata reads require existing gcloud access to describe services/revisions.
`--gcloud` can name the installed binary if it is not on PATH.

Origins must be HTTPS without credentials, paths, query strings or fragments.
Only literal loopback origins may use HTTP for local tests. Each origin must
equal that service's canonical `status.url` before the bearer is sent. Custom or
tagged URLs cannot stand in for the serving production origin. Redirects are
never followed, including same-origin redirects. Tokens stay in HTTP headers in
memory, never in subprocess arguments, diagnostics or JSON reports. Raw HTTP
bodies and gcloud output are not copied into the report.
Each complete HTTP operation has a ten-second deadline, including a slow body;
each gcloud process has a thirty-second deadline. A timed-out probe fails closed.

## Evidence and failure behavior

For **each** service the verifier:

1. Reads Cloud Run's current service generation, Ready condition, resolved
   `status.traffic`, and the revision's service label and resolved image digest.
   All traffic must be accounted for by the expected revision and total 100%.
   Zero-percent tagged entries for another revision also fail.
2. Runs the four legacy unauthenticated routing checks and requires an invalid
   bearer to fail `/auth/me` with 401.
3. Requires the real bearer to return a valid `/auth/me` account/membership shape,
   with the expected user/campus identity and no suspension.
4. Reads authenticated `/_deployment`, requiring the service and revision names
   to match and **both** code/database head lists to equal the single expected
   migration revision. Missing, empty, multiple, older or newer heads fail.
5. Requires a successful authorized campus Chirps query (`limit=1`) and validates
   returned rows against the current anonymous feed contract. An empty campus
   is allowed; this proves an authorized query, not populated-row serialization.

After probing both services it reads the Cloud Run metadata again. An observed
change or incomplete evidence prevents readiness. Failures return exit 1 with
`NOT_READY` and fixed diagnostic labels/status codes. Success returns exit 0 with
`AUTHENTICATED_READY`; routing-only runs also return 0 but explicitly report
`ROUTING_ONLY`. Automation must require the authenticated verdict, not merely
exit 0 from an invocation lacking the authenticated inputs.

The endpoint returns only non-secret service/revision names and migration heads.
It uses the ordinary registered-user authentication guard. Packaged heads come
from the image's Alembic files, and database heads are read from `alembic_version`
on each call. `/_health` remains DB-free liveness. Deploy the endpoint to both
services before expecting the new authenticated verifier to pass.

## Scope of the result

This establishes migration **metadata** agreement and the tested authenticated
HTTP contracts at the observed deployment. It is not a physical-schema audit,
load/cost test, proof of every route, or proof against a later traffic change.
HTTP checks on `chirp-ws` do not exercise the WebSocket upgrade, subscriptions,
Redis fan-out or client reconnection; those still need their separate checks.
This local implementation run does not establish production readiness.

Automated regressions execute the real CLI against two local HTTP peers and a
fake gcloud executable, including bad/expired bearer, healthy routing with bad
contracts, wrong/missing heads, wrong origins/digests, stale generations, traffic
splits, redirects and credential-safe failure artifacts. Endpoint tests execute
the actual Firebase guard (only token verification mocked) and a disposable local
PostgreSQL database, including actual head drift and ordinary non-admin access.

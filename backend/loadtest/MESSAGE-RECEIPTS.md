# Local message delivery measurement (c363)

`python -m loadtest.message_receipts` measures HTTP-created messages arriving on
an explicitly selected recipient WebSocket cohort while the existing HTTP mix
runs. It is an opt-in, loopback-only instrument. The ordinary `python -m loadtest`
command and its historical reports keep their existing semantics.

Each recipient must receive the gateway's exact ready frame before HTTP warmup
starts. The producer then waits for an actual response from the active HTTP mix,
posts opaque synthetic bytes through `/conversations/{id}/messages`, and binds a
validated HTTP 201 message ID to each selected recipient's matching message event.
The event can arrive before the HTTP response; the instrument buffers bounded
correlation data until that response establishes an accepted message.

This measures arrival in a Python client. It does not write the server's
`/messages/{id}/receipts` rows, decrypt a message, exercise a phone, prove durable
recovery, or establish production capacity. The remote-load park, staging window,
and c287 HA decision in [RUNBOOK.md](RUNBOOK.md) remain in force.

## Disposable fixture

Use a dedicated local PostgreSQL database, local Redis process, and API process
from the reviewed checkout. Do not use another session's process or port. The
fixture helper requires an explicit `postgresql+asyncpg` URL with literal
`127.0.0.1` or `::1`, an explicit port, no query or fragment, and database name
`chirp_receipts_` followed by 8–32 lowercase hexadecimal characters. It also
requires `ENV=local` and `AUTH_MODE=emulated` explicitly.

For example, from `backend/`, with your existing virtual environment activated:

```sh
umask 077
CHIRP_RECEIPT_RUN="$(python -c 'import uuid; print(uuid.uuid4().hex)')"
CHIRP_RECEIPT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/chirp-receipts.XXXXXX")"
CHIRP_RECEIPT_DB="chirp_receipts_$CHIRP_RECEIPT_RUN"
createdb -h 127.0.0.1 -p 5432 -U chirp "$CHIRP_RECEIPT_DB"
export ENV=local AUTH_MODE=emulated SERVICE_ROLE=all
export DATABASE_URL="postgresql+asyncpg://chirp:chirp@127.0.0.1:5432/$CHIRP_RECEIPT_DB"
alembic upgrade head
python -m loadtest.receipt_fixture --recipients 2 --out "$CHIRP_RECEIPT_DIR/manifest.json"
```

The database must contain no existing application identities, campuses, chapters,
conversations, or devices. The helper creates a fresh verified campus/chapter,
sender, selected recipients, active group membership, and a sender device. Its
synthetic device directory row and opaque payload are transport fixtures; they
do not establish a cryptographic session. No Firebase account, push token, or
external service is provisioned. Fixture identities and UUIDs stay in the private
manifest; the report contains aggregates.

The helper reserves a new mode-0600 manifest before opening its transaction and
never overwrites a file or follows a final-component symlink. A failure after
database commit can leave an empty or partial file. Inspect the disposable
database and output before rerunning; failure is not proof that nothing changed.

Start a dedicated Redis listener on an unused loopback port, then start the local
API with that `REDIS_URL` and the same explicit database and auth settings. For an
already reserved local Redis port 6395 and unused API port 8010, for example:

```sh
export REDIS_URL=redis://127.0.0.1:6395/0
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

The command above does not start Redis. Without a real subscribe acknowledgement,
the gateway's early 4503 close is a failed run, never a successful connection hold.
Keep local server diagnostics private; app logging can include fixture IDs.

## Run the instrument

In another terminal using the same checkout and virtual environment:

```sh
python -m loadtest.message_receipts \
  --config loadtest/message-receipts-config.yaml \
  --manifest "$CHIRP_RECEIPT_DIR/manifest.json" \
  --messages 3 --interval-seconds 4 --settle-seconds 2 \
  --out "$CHIRP_RECEIPT_DIR/report.json"
```

Carry `CHIRP_RECEIPT_DIR` into that terminal explicitly. Use a new report path for
each invocation. The example HTTP mix reads `/auth/me`; the retained Runner
warmup can create up to five synthetic posts before active mix traffic begins.
Those warmup calls are not counted as concurrent message-workload responses.

Only `http://127.0.0.1:port` or `http://[::1]:port` API origins and equivalent
`ws://...:port/ws` endpoints are accepted. URL credentials, query strings,
fragments, redirects, proxy environment settings, and Firebase auth are refused
or disabled. Approval settings cannot enable a remote target in this command.

Bounds include 20 declared users, 10 selected recipients, 10 messages, a 120-second
HTTP duration and a 180-second overall deadline. Messages are serial, with at
least four seconds between actual dispatch starts: 15/minute, half the current
`message_send` limit of 300/600 seconds. Sends share the HTTP Runner's global
token bucket and in-flight semaphore. The retained reference probe adds up to
one request/second and one in-flight request outside those caps. There are no
message retries; a rejected or unconfirmed send stops the producer and does not
become a successful receipt expectation.

The shared token bucket retains its initial burst of `max(2, max_rps)` tokens;
its refill rate is not a promise about every rolling one-second interval.
`ws.max_sockets` caps the selected recipient cohort and `connects_per_second`
paces its connections. This command keeps that cohort through the HTTP phase and
receipt observation period; the default command's `hold_seconds` is not its
socket lifetime.

## Read the result

The denominator is **validated HTTP-accepted messages × selected recipients**.
A missing recipient never disappears from that denominator because its socket
closed. Each `(recipient, message)` contributes at most one unique receipt;
duplicates are counted separately. The selected cohort is not a claim about
every member or device in the conversation.

Reconciliation binds conversation ID, message ID, sender device and payload in
memory. Unknown, malformed or mismatched events cannot satisfy an expectation.
The settle deadline is anchored to the final send response; observations after
it cannot repair a missing receipt. Internal state and total observed frames
are bounded; exceeding a bound invalidates the run instead of silently dropping
evidence.

Latency is monotonic **request-start to client arrival**, including API work,
broker delivery, network and driver scheduling. Arrival before the HTTP response
is valid and retains its original timestamp. These numbers are neither server
latency nor a proof that the driver is unsaturated.

Inspect actual active-mix HTTP response overlap, accepted sends, expected/unique/
missing/duplicate receipts, early closes, aborts and completeness together. A
handshake, zero expectations, cancellation, partial run or missing recipient
cannot establish complete local delivery. Valid duplicates do not count as new
unique deliveries. Production capacity, durable recovery, decryption and device
acceptance remain unproven even for a complete local run.

Afterward, stop only the processes you started, preserve the private report, and
drop only this run's disposable database. No shared-data cleanup query is needed.

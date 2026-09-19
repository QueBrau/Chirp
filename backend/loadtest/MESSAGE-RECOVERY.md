# Local message recovery after disconnect (c363)

`python -m loadtest.message_recovery` exercises a deliberate client disconnect
against the real local message API and WebSocket gateway. It uses the disposable
fixture and explicit loopback/emulated-auth configuration described in
[MESSAGE-RECEIPTS.md](MESSAGE-RECEIPTS.md). The existing receipt command and default
load harness keep their behavior.

## Run

First create the disposable fixture and start its dedicated local API and Redis
processes using the receipt guide. Then, from `backend/`:

```sh
python -m loadtest.message_recovery \
  --config loadtest/message-receipts-config.yaml \
  --manifest "$CHIRP_RECEIPT_DIR/manifest.json" \
  --interval-seconds 4 --settle-seconds 2 \
  --out "$CHIRP_RECEIPT_DIR/recovery-report.json"
```

The instrument sends exactly three opaque synthetic messages:

1. Wait for every selected recipient's gateway-ready frame, start the HTTP mix,
   and prove every recipient receives the first HTTP-accepted message.
2. Deliberately close every selected recipient with a normal close handshake.
   Wait for those transports and receiver tasks to finish, then accept a second
   message while all selected recipients are offline.
3. Connect a fresh recipient cohort and require new gateway-ready frames. As
   each recipient, read authenticated message history and match the second
   message to its accepted ID, conversation, sender device, payload, type and
   timestamp. Finally send a third message and prove live delivery to every
   selected recipient again.

The existing HTTP mix runs through these phases. Each live observation window and
the offline interval must contain an actual successful mix response; starting a
task or setting an active flag does not establish overlap. History reads and the
three sends share the mix's global pacing and in-flight caps. There are at least
four seconds between actual send starts, and there are no send or reconnect
retries. An unconfirmed send stops the sequence rather than risking a duplicate.

The example's 20-second HTTP duration suits the two-recipient fixture. Larger
cohorts or slower connection ramps need an explicitly longer duration, within
the existing 120-second maximum, to cover both readiness ramps, send spacing,
history reads and settle windows. If the HTTP phase ends too early, the run
fails; the instrument does not silently extend its workload.

## Read the result

Only `LOCAL_RECONNECT_RECOVERY_OBSERVED` with exit 0 establishes this local
sequence. Failures produce `NOT_PROVEN`; refused inputs also exit nonzero.

The live denominator is fixed at **two messages times the selected recipient
count**. The history denominator is **one offline message times that same
count**. Neither denominator shrinks when a send fails, a recipient disconnects,
or a history lookup omits the accepted message. Seeing that message later on a
WebSocket cannot replace the independent history check.

Inspect phase completion, missing live/history observations, actual dispatch
intervals, per-phase HTTP response counts, unexpected closes and aborts together.
An intentionally closed first connection cannot excuse an abnormal close or a
failure of its replacement. Receipt timestamps are checked against each live
phase's settle deadline, so late frames cannot repair a missing receipt.

History is one bounded request per selected recipient, examining at most 20 rows
and 64 KiB per response. It is a check of the current run's accepted offline
message, not an exhaustive history synchronization or pagination test. The
report includes client request-to-validated-history latency; this is neither
server-only latency nor an operational recovery-time objective.

Reports contain aggregates, never fixture identities, tokens or ciphertext.
The output must be a new file and is created with mode 0600. Existing files and
final-component symlinks are refused before traffic starts. Keep local server
diagnostics private and clean up only this run's disposable resources.

## Limits of the evidence

The command accepts only literal loopback targets and emulated authentication;
approval flags cannot enable remote use. It retains the receipt instrument's
20-user, 10-recipient, 20 RPS, 10 in-flight request and 180-second overall bounds.
The existing reference probe is separately disclosed at up to one additional
request per second and one additional in-flight request. The shared token bucket
has its documented initial burst; its refill rate is not a rolling-second limit.

This proves recovery through authenticated durable history after deliberate
client disconnection. It does not stop Redis or the server, restore a database,
exercise a native phone, decrypt messages, create server receipt rows, or prove
exactly-once delivery, production capacity or RPO/RTO. The remaining c363 mixed
workload, cardinality, fairness, failure-drill and device evidence stays open.
The remote-load approval and c287 HA decision in [RUNBOOK.md](RUNBOOK.md) remain
in force.

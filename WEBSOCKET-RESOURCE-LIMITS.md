# WebSocket resource limits — c354 partial backend delivery

This change bounds the gateway's outbound application work and waiting tasks.
c354 remains open: the coordinated client slice and its release/device acceptance
are tracked in [REALTIME-CLIENT-RECOVERY.md](REALTIME-CLIENT-RECOVERY.md). The
inbound transport's empty-continuation-fragment retention issue, described
below, is now closed by construction with `BoundedFragmentWebSocketsProtocol`,
but is not live in production until the next Cloud Run deploy window. These
results do not establish a safe production user count or a total per-socket
memory ceiling.

## Gateway contract

| Resource / operation | Limit |
| --- | --- |
| Outstanding outbound frames, including the sender's frame | 32 |
| Outstanding outbound UTF-8 payload bytes, including the sender's frame | 512 KiB |
| One outbound encoded frame | 128 KiB |
| Admission-to-send-completion age | 5 seconds |
| One send await | 5 seconds, reduced by age already spent queued |
| Redis subscribe command plus actual subscription ACK | 2 seconds |
| Firebase verification caller | 10 seconds; shared completion-owned four-worker admission |
| Initial user lookup or one authorization reconciliation | 2 seconds |
| Periodic reconciliation sleep | 30 seconds with ±20% jitter, independently of Redis |
| Close attempt | 1 second |
| Worker cancellation / Redis close | Separate 1-second budgets |

The subscriber and sender are independent tasks. Admission never waits for queue
space. Overflow, oversize, expiration, broker failure, or an unsuccessful send
ends that stream with a best-effort 4503 close. No message is truncated. Logging
contains reason/type, user ID and peak counters, never tokens or message bodies.
Only frames that completed sending release their ownership normally; cancellation
releases the in-flight frame and discards the remaining transient queue.

The 128-KiB frame ceiling accommodates the current 64-KiB ciphertext input cap
plus its JSON envelope. Legacy oversized events disconnect the transient stream;
their stored HTTP representation is unchanged. The byte quota counts encoded
payload, not Python object overhead, Redis-parser allocations, network buffers,
or process RSS. Redis must already parse an event before admission can reject it.

Authentication SQL is released before accepting or rejecting a socket. Each
periodic lookup uses a fresh short session and distinguishes an absent user from
an existing row whose suspension field is NULL. A missing account ends the stream
with 4401, suspension with 4403, and a failed/timed-out check with 4503. All readers
and senders are canceled before the close attempt. Bytes already given to the
network cannot be recalled. With a healthy scheduler, the next check starts within
36 seconds, then has its 2-second query budget, separate 1-second worker
cancellation budget, and 1-second close-attempt budget;
this is not a hard network or operating-system revocation deadline.

The token verifier uses c355's shared executor. Caller cancellation does not free
a slot held by a still-running native verification call. Four SDK calls may stay
occupied until the SDK completes; admission waiters remain part of the server's
overall request-concurrency envelope. No SDK-duration bound is claimed.

## Readiness and coordinated client release

The gateway sends an additive `{"type":"ready"}` event only after reading the
actual Redis `subscribe` ACK for that user's channel. Awaiting redis-py's
`subscribe()` alone is insufficient: the installed client sends the command
without reading that reply. A later automatic resubscription ends the connection
so missing events cannot be hidden behind an apparently unchanged stream.

This event marks subscription readiness, not a durable-delivery receipt. Older
clients ignore the unknown event and still treat native `onopen` as ready. Deploy
the additive server event before releasing clients that require it; a fallback
to native `onopen` would restore the history/subscription race. The coordinated
client contract and its separate source/release evidence are in
[REALTIME-CLIENT-RECOVERY.md](REALTIME-CLIENT-RECOVERY.md).

The coordinated c354 client contract preserves identity/operation ownership
protections and the existing history/inbox work:

- Connect only for an authenticated foreground app or visible browser tab;
  cancel reconnect timers while backgrounded, and revalidate before resuming.
- Treat the server's ready event as usable realtime, with a readiness timeout.
- On every ready event, including first connection, refresh a bounded durable
  thread/inbox window. Resolve live message IDs through current server visibility
  checks; callback arrival order never makes a raw event authoritative. Retain
  server cursors, exact ordering and stale-result rejection.
- Exercise real handlers for background/resume, suspended/unsuspended accounts,
  reconnect churn and missed durable updates; device integration remains separate.

Redis pub/sub remains transient. The real local integration test stores a message
while the recipient is disconnected, then proves the HTTP history recovers it
after ready and the resumed socket receives the next committed message. That does
not prove multi-page client catch-up or delivery during a publish failure. c356's
durable outbox remains a separate implementation.

## Transport limits and the inbound continuation-fragment bound

The container CMD selects `app.ws.transport:BoundedFragmentWebSocketsProtocol`
(`backend/app/ws/transport.py`), a thin subclass of the same installed
`WebSocketsSansIOProtocol` (`websockets-sansio`) adapter Uvicorn 0.52.3
auto-selection used. Its maximum incoming message payload is still 1024 bytes and
per-message compression is still disabled. The stream has no legitimate incoming
application messages today; ping/pong control traffic remains functional. Actual
loopback tests send 1024-byte text successfully and reject oversized ASCII,
multi-byte UTF-8 and fragmented-message payloads with 1009.

`--ws-max-queue` is deliberately absent: this pinned adapter does not read that
configuration option, and switching to the legacy adapter was rejected after an
actual-library proof showed its close coroutine suppressing cancellation and
outliving the proposed close-attempt budget.

The stock adapter retains each nonfinal fragment in a Python list with only a
joined-byte-total check that runs after FIN, so a sender that starts a message
(fin=False) and never sends FIN can grow that list without limit while every
individual frame and the running byte total stay under `--ws-max-size`: a
one-byte initial text fragment followed by 1000 then 2000 empty nonfinal
continuation frames retained 1001 then 2001 fragment entries. This was a
confirmed unbounded-fragment-bookkeeping path, not merely uncertainty about RSS,
and an independent direct-adapter probe also reproduced retention before ASGI
accept while application authorization was pending.

c354 closes that gap by construction. `BoundedFragmentWebSocketsProtocol`
overrides only `handle_cont` to count `self.frames` before appending and, once
`WS_MAX_CONTINUATION_FRAMES` (64) nonfinal continuation frames are retained,
sends a 1009 close and stops -- the same close path the base class already uses
one method away for a different malformed-input case. `self.frames` itself never
exceeds the bound; a `close_sent` guard (found necessary at runtime: without it,
a further CONT frame reaching `handle_cont` after the first close call raises
`websockets.exceptions.InvalidState` from `conn.send_close` on an
already-CLOSING connection) makes the no-op on repeat calls explicit rather than
relying on the transport tearing down before another frame is parsed. This is an
app-level fix, not a fork of Uvicorn or websockets, and does not touch
`--ws-max-size` or `--ws-per-message-deflate`. The pre-accept sub-case (retention
observed before ASGI accept) is covered by construction -- the override sits at
the same protocol layer, below any app-level accept decision -- but is not
separately tested.

Runtime-verified 2026-09-11 from the worktree backend on loopback ports (not
8080/8000): a real `uvicorn` process started with the exact Dockerfile CMD
`--ws` value answered `GET /_health` 200, then a raw-socket client that opened a
text frame (fin=False) and sent exactly 64 empty continuation frames (never FIN)
was closed with code 1009 and `len(protocol.frames)` stayed at the bound. The
identical byte sequence against a server started with `--ws websockets-sansio`
(stock) produced no close and kept retaining fragments. This fix is **not live
until the next Cloud Run deploy window**; the c361 deploy verifier's WS check is
expected to exercise `BoundedFragmentWebSocketsProtocol` on that window.

An isolated follow-up confirmed the same unbounded-append shape still exists in
Uvicorn 0.52.4 and websockets 17.1 (the latest available releases as of
2026-09-10): the [adapter still appends fragments until FIN](https://github.com/Kludex/uvicorn/blob/0.52.4/uvicorn/protocols/websockets/websockets_sansio_impl.py#L272),
and the [parser's payload-size check](https://github.com/python-websockets/websockets/blob/17.1/src/websockets/protocol.py#L671)
does not cap empty-fragment count -- a dependency bump alone would not have fixed
this, and this slice changes neither lockfile nor transport internals.

`--include-fragment-evidence` runs a bounded 2000-continuation diagnostic
against a directly-imported, hardcoded `WebSocketsSansIOProtocol` instance (not
`config.ws_protocol_class`, so not whatever class the container CMD's `--ws`
value actually resolves to), recording whether the stock library's known
limitation is still observed there. It intentionally keeps diagnosing the
underlying stock adapter regardless of the shipped `--ws` value, so
`known_limitation_observed` continues to read `True` by design -- that is the
baseline evidence this fix responds to, not a regression check on the shipped
class. The shipped class is instead covered by
`backend/tests/test_c354_inbound_fragment_bound.py`'s parametrized bounded-vs-stock
contrast and by `test_ws_resource_integration.py::test_real_container_cli_selects_effective_frame_limit`,
which importlib-imports the Dockerfile's actual `--ws` string and asserts it IS a
`WebSocketsSansIOProtocol` subclass. It is deliberately not a failing normal-CI
test and does not make the issue part of the desired runtime contract.

### Launch-point sweep (chirps-17 condition 4)

Every place in the repo that starts or configures a real Uvicorn WS listener:

| Launch point | Resolves to | Why |
| --- | --- | --- |
| `backend/Dockerfile` CMD (production/Cloud Run) | `BoundedFragmentWebSocketsProtocol` | The fix; ships the bound. |
| `backend/loadtest/ws_resource_probe.py`'s `local_server()` / `container_transport_options()` (used by the probe CLI and by `backend/tests/test_ws_resource_integration.py`'s integration tests) | `BoundedFragmentWebSocketsProtocol` | Reads the Dockerfile CMD directly, so it always matches production. |
| `backend/loadtest/ws_resource_probe.py`'s `fragment_bookkeeping_evidence()` diagnostic | stock `WebSocketsSansIOProtocol` (hardcoded import, not read from config) | Deliberate: this diagnostic exists to keep showing the underlying library's unbounded behavior as baseline evidence, independent of whatever the app ships. Stock is acceptable here because the function is explicitly a non-CI, non-regression diagnostic (see above), never the production path. |
| `backend/tests/test_c354_inbound_fragment_bound.py` (STOCK parametrization only) | stock `WebSocketsSansIOProtocol` (explicit override) | Deliberate known-broken baseline, parametrized alongside the shipped class, to prove the fix by contrast under the identical frame sequence. Never the production path. |
| `docker-compose.yml` | n/a | Only runs `db` (Postgres) and `redis` containers for local dev; it does not start the backend/Uvicorn at all. |
| `.github/workflows/ci.yml` | n/a | CI never invokes `uvicorn` directly; it runs pytest, which reaches Uvicorn only through the two rows above. |
| `scripts/*` | n/a | No script in this repo starts Uvicorn. |

With the selected sansio adapter, a writable close is queued promptly and has a
separate native 10-second physical-close timer. If flow control blocks the close,
the gateway's attempt times out and ASGI return causes Uvicorn to close the
transport. The actual loopback blocked-write test measures this path. A client
that cannot receive the close frame may observe 1006 instead of the intended
4503/4403. Pre-accept rejection becomes an HTTP 403 handshake failure on the real
network; TestClient's pre-accept application close code is not a browser guarantee.

Task/Redis cleanup relies on cancellation behavior of the pinned libraries, which
is tested for these paths. An arbitrary replacement coroutine that suppresses
cancellation is outside this deadline proof. Redis `aclose()` disconnects and
returns the subscription connection; no extra UNSUBSCRIBE network round trip is
required. The physical OS close may finish after application ownership ends.
Teardown is shielded from AnyIO's repeated outer cancellation while its own
operation deadlines remain active. Both direct task cancellation and AnyIO
level-cancellation regressions assert worker/subscription cleanup.

## Reproducing the local measurements

The Redis 7.2.15 source archive came from
[Redis's release server](https://download.redis.io/releases/redis-7.2.15.tar.gz).
SHA-256 `7bf7975331511fdb788e85dae63964b128fccee1df026a10db57444babc9c9c4`
was checked against the [official hash manifest](https://raw.githubusercontent.com/redis/redis-hashes/master/README)
before extraction. The local build used `make -j2 MALLOC=libc BUILD_TLS=yes` and
the existing OpenSSL 3 prefix, entirely under `/private/tmp`; no system install.
On Jose's Mac, its child environment needs `LC_ALL=C LANG=C`.

Run a dedicated loopback Redis on an unused port with persistence off, 64 clients,
64-MiB memory configuration, and pub/sub output limits of 2 MiB hard / 512 KiB soft
for 2 seconds. Track and stop its own PID. c372's TLS fixture uses a separate
process/port. Redis maxmemory alone is not an aggregate pub/sub memory guarantee.

From `backend`, using the test virtualenv:

```sh
python -m loadtest.ws_resource_probe \
  --redis-url redis://127.0.0.1:YOUR_FIXTURE_PORT/0 \
  --database-url postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test \
  --sockets 16 --rounds 3 --hold-seconds 38 \
  --report /private/tmp/chirps-ws-measurements.jsonl \
  --include-fragment-evidence
```

The CLI rejects non-loopback endpoints and non-test database names. The existing
pytest fixture creates and drops its own marked run database. All WebSocket
clients explicitly bypass proxies. The CLI permits at most 32 sockets, 5 rounds
and 60 seconds in its first hold; later churn rounds are short. The slow-reader
probe publishes at most 64 frames / roughly 4 MiB within 12 seconds and shrinks
only its local OS/write-buffer budgets to reproduce backpressure cheaply.

The probe parses the actual Docker command through installed Uvicorn's CLI. It
records actual delivery IDs/latency, Redis subscription/connection/output counts,
Redis memory, process RSS, SQL query counts and checkout ownership with a fixed
one-connection/no-overflow database pool. It asserts surviving HTTP service and
zero subscription/gateway-task/SQL-checkout residue after each churn round.
Process RSS includes clients, server, pytest and the ORM in this shared test
process; it is not a per-user cost estimate. Heap tracing was excluded after a
controlled comparison showed it stalling simultaneous local handshakes; server
deadlines were not increased to hide that observation cost.

The local 2026-09-08 run used Redis 7.2.15, Uvicorn 0.52.3, websockets 17.0.1,
Python 3.11 and PostgreSQL 14 on Jose's Intel Mac. Its measurement CLI passed all
9 integration cases in 52.83 seconds. The first hold retained the production
30±20% cadence; only the separate mid-session suspension case accelerated polling.
After final AnyIO teardown hardening, the combined 20 controlled gateway cases,
9 native integration cases and 21 existing WebSocket regressions passed: 50 tests
in 51.21 seconds, with zero skips. That final regression run uses accelerated
polling; the unchanged production cadence is covered by the separate hold above.

| Observed local fixture result | Measurement |
| --- | --- |
| Sockets / reconnect rounds | 16 / 3 |
| Received delivery checks | 48 of 48; maximum latency 5.53 ms |
| User reconciliation SELECTs during the first 38-second hold | 16 |
| Maximum simultaneous database checkouts | 1, with zero overflow configured |
| Redis subscriptions while open / after each round | 16 / 0 |
| Remaining gateway tasks and SQL checkouts after each round | 0 / 0 |
| Process RSS across the three open-cohort samples | 127,397,888 → 129,175,552 bytes |
| Redis allocated memory after the three cleanup samples | 656,608 / 690,368 / 694,464 bytes |
| Slow-reader application buffer peak | 8 frames / 491,897 bytes |
| Slow-reader release / healthy-peer deliveries | 244.5 ms / 16 events |
| Maximum observed Redis output / local transport write buffer in that slow-reader case | 0 / 61,593 bytes |
| Native blocked-write close test | 41.5 ms with a 40-ms test attempt budget |

These are finite local observations. Allocator/cache retention and the client/test
process are included; three rounds do not establish a memory-leak absence or a
production scaling curve. The fast-peer survival regression also requires a fresh
marker after the subscription count falls, so count alone cannot identify the
survivor. Raw measurement JSON is written to the explicit local report path; no
credentials or message contents are included in those records.

Retain per-socket subscriptions and periodic SQL for this slice. With N stable
sockets, the existing design performs approximately N/30 reconciliation queries
per second plus authentication work; this formula is not a measured user ceiling.
Compare the local results with realistic deployment concurrency, Redis output
limits, pool budgets and message sizes before deciding whether shared process
subscriptions or cached suspension invalidation justify their complexity. Any
optimization must keep bounded reconciliation when Redis is unavailable.

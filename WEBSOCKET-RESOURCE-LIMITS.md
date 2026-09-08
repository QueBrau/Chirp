# WebSocket resource limits — c354 partial backend delivery

This change bounds the gateway's outbound application work and waiting tasks.
c354 remains open: foreground/visibility lifecycle and durable consumer catch-up
need coordinated mobile work, and the current inbound transport has a confirmed
empty-continuation-fragment retention issue described below. These results do
not establish a safe production user count or a total per-socket memory ceiling.

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

## Readiness, reconnects and remaining client work

The gateway sends an additive `{"type":"ready"}` event only after reading the
actual Redis `subscribe` ACK for that user's channel. Awaiting redis-py's
`subscribe()` alone is insufficient: the installed client sends the command
without reading that reply. A later automatic resubscription ends the connection
so missing events cannot be hidden behind an apparently unchanged stream.

This event is a subscription fence, not a durable-delivery receipt. Existing
clients ignore the unknown event and still treat native `onopen` as ready. Their
current catch-up/subscription race is therefore not fixed until the coordinated
client slice lands. Deploy the additive server event before updated clients
require it; a fallback to native `onopen` would restore the race.

The remaining c354 client work must preserve the existing identity/operation
ownership protections and Claude's history/inbox work:

- Connect only for an authenticated foreground app or visible browser tab;
  cancel reconnect timers while backgrounded, and revalidate before resuming.
- Treat the server's ready event as usable realtime, with a readiness timeout.
- On every ready event, including first connection, refresh durable thread/inbox
  state, merge by stable ID without overwriting concurrent live events, and retain
  bounded pagination and stale-result rejection.
- Exercise real handlers for background/resume, suspended/unsuspended accounts,
  reconnect churn and missed durable updates; device integration remains separate.

Redis pub/sub remains transient. The real local integration test stores a message
while the recipient is disconnected, then proves the HTTP history recovers it
after ready and the resumed socket receives the next committed message. That does
not prove multi-page client catch-up or delivery during a publish failure. c356's
durable outbox remains a separate implementation.

## Transport limits and confirmed open issue

The container explicitly selects the same installed `websockets-sansio` adapter
that Uvicorn 0.52.3 auto-selection used. Its maximum incoming message payload is
1024 bytes and per-message compression is disabled. The stream has no incoming
application messages today; ping/pong control traffic remains functional. Actual
loopback tests send 1024-byte text successfully and reject oversized ASCII,
multi-byte UTF-8 and fragmented-message payloads with 1009.

`--ws-max-queue` is deliberately absent: this pinned adapter does not read that
configuration option. It pauses transport reads after a complete message, but may
already have parsed other frames in the received chunk. No strict inbound frame
count is established. Switching to the legacy adapter was rejected after an
actual-library proof showed its close coroutine suppressing cancellation and
outliving the proposed close-attempt budget.

More concretely, the current sansio adapter retains each nonfinal fragment in a
Python list. A one-byte initial text fragment followed by 1000 then 2000 empty
nonfinal continuation frames retained 1001 then 2001 fragment entries while total
payload stayed one byte. The 1024-byte limit did not fire, reads did not pause and
the transport stayed open. This is a confirmed unbounded-fragment-bookkeeping
path, not merely uncertainty about RSS. It remains OPEN on c354 and needs a
separately reviewed upstream/transport mitigation. This change does not fork
Uvicorn, install private parser hooks, or claim to fix that path.
An independent direct-adapter probe also reproduced retention before ASGI accept
while application authorization was pending. Production ingress/proxy forwarding
of that traffic was not tested; the issue is not limited here to authorized users.
An isolated follow-up also reproduced it with Uvicorn 0.52.4 and websockets 17.1:
the [adapter still appends fragments until FIN](https://github.com/Kludex/uvicorn/blob/0.52.4/uvicorn/protocols/websockets/websockets_sansio_impl.py#L272),
and the [parser's payload-size check](https://github.com/python-websockets/websockets/blob/17.1/src/websockets/protocol.py#L671)
does not cap empty-fragment count. A dependency bump alone is not a verified fix;
this slice changes neither lockfile nor transport internals.

`--include-fragment-evidence` runs a bounded 2000-continuation diagnostic through
the actual installed parser and ASGI task with a controlled transport, recording
whether that known issue is still observed. It is deliberately not a failing
normal-CI test and does not make the issue part of the desired runtime contract.

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

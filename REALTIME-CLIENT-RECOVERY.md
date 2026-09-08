# Realtime client recovery — c354

This is the contract for the coordinated client slice. Source tests and the
release checks below are separate evidence. c354 stays open for device validation
and the inbound transport issue in
[WEBSOCKET-RESOURCE-LIMITS.md](WEBSOCKET-RESOURCE-LIMITS.md). Redis pub/sub remains
transient; this change does not implement c356's durable outbox or promise that
every update is delivered live.

## Readiness and ownership

The socket becomes usable only after the server's exact `{"type":"ready"}`
message. The ten-second connection deadline includes waiting for this message.
Duplicate ready messages and application events received before readiness do not
create new ready transitions. A native connection opening is not sufficient.

An authenticated run retains its existing transport and authentication retry
budgets across background/foreground changes. Only a known active native app or
visible browser tab may connect. A foreground transition owns its revalidation,
socket callbacks and retry timers; superseded transitions cannot publish results.
Explicit retry and genuine identity changes retain their existing reset behavior.
These client protections do not replace server membership or suspension checks.

A screen's durable reads must belong to the current identity and focused route.
Blur, account change, unmount and leaving a conversation retire that read owner.
Focus refreshes the authoritative window even if the socket remained ready.
React effect cleanup/setup replay must establish a usable new read lifecycle
without letting the old operation publish into it.

## Message history and live hints

A full thread refresh reads at most four pages of 50 messages within one
15-second operation. Initial focus, a new ready transition and explicit refresh
start this operation. It replaces the bounded window, so rows removed by current
server visibility do not survive merely because they were cached. A full final
page means more history may exist; the UI provides continuation and explains that
older history may include updates missed while offline. This is not a proof that
the client has recovered all history.

Live frames supply IDs, never authoritative message rows or ciphertext. The
client resolves coalesced IDs through:

```text
GET /conversations/{conversation_id}/messages/by-id?ids=uuid1,uuid2
```

The decoded CSV accepts 1–50 canonical hyphenated UUIDs, case-insensitively, up to
1,849 characters. Repeated `ids` query keys are rejected. Count and length apply
before deduplication; malformed input is rejected before authentication/database
dependencies. Successful responses are plain `MessageOut[]`, ordered by
`(created_at, id)` descending. History and lookup share current membership and
named-block visibility rules. Missing, filtered and foreign-conversation IDs all
produce no corresponding row. This does not promise constant response timing.

Pending IDs and IDs in flight are each capped at 50. Live resolution permits one
follow-up and spaces batch starts by at least 500 ms. Overflow, continuing
activity beyond that batch budget, or an unresolved read is shown as incomplete;
the client does not silently claim to be current. Only canonical returned rows
can update the requested IDs. Normal live events do not trigger a 200-row history
refresh.

Pagination uses a cursor returned by the server, preserving timestamp
microseconds and UUID ordering. Do not invent an `after newest timestamp` cursor:
PostgreSQL `now()` is transaction-start time, so a later commit can have an older
`created_at`. A new event ID can resolve that row directly even when it sorts
behind the newest cached message. Historical pages remain available separately.

The inbox uses a 30-row summary head refresh for coalesced live activity. It
replaces the covered head interval while retaining loaded older rows outside that
interval. Hints outside the covered head must remain visibly unresolved. Inbox
summaries do not download message history or ciphertext bodies.

## Poll reconciliation

A ready transition refreshes the currently held poll window, capped at four
50-row pages and one follow-up within a single 15-second operation. Overlapping
events or successful local mutations invalidate that read. The follow-up must
obtain a current server result; event arrival time alone does not establish which
absolute tally is newer. Continued activity leaves an explicit incomplete state.

Refresh keeps the existing own-vote, mutation and deletion protections. Server
rows supply the caller's ballot; broadcast events do not contain another person's
ballot. A final authentication or access refusal clears protected rows and titles. The UI
uses neutral access-unverified wording and an explicit retry owned by the current
focus and identity; a 401 alone does not claim membership revocation or sign out
Firebase. Cancellation reaches the HTTP request as well as its outer wait.

## Cost and privacy limits

ID/row bounds are not byte bounds for legacy stored rows. At the current
65,536-character ciphertext input cap, a single-message lookup transfers roughly
one message envelope instead of four pages of history. Up to 50 distinct IDs can
still produce a substantial response. An executed synthetic fixture containing 200 such messages transferred over
13,000,000 application JSON bytes during initial history loading; one later live
hint fetched one canonical row under 66,000 bytes. These are captured application
JSON sizes, excluding HTTP/TLS overhead, compression and device behavior. They are
not measured mobile bandwidth, battery use, cloud billing or production capacity.

Previously delivered content cannot be recalled. Current server visibility is
checked when an authoritative read occurs; this slice does not guarantee instant
revocation of every cached row across every open screen. It also does not add
message decryption or enable the parked sending/E2EE work.

## Release and acceptance

1. Use [DEPLOY-CONFIGURATION.md](DEPLOY-CONFIGURATION.md) to verify the paired API
   and WebSocket release, actual serving revisions, migration head and connection
   envelope. Source merge alone does not change deployed services.
2. Deploy and verify the gateway's subscription-ready event and the API's dedicated
   lookup route before releasing updated clients. An old API returns 404 for the
   new route; silently falling back to raw event payloads is not compatible.
3. Verify a real authenticated client route reaches the intended WebSocket service,
   receives readiness after subscription, and reads canonical history from the API.
4. On native devices and web, check background/foreground churn, focus/back
   navigation, account switch, cancellation, denied membership, leave, missed
   updates beyond one page, offline return and a continuing live burst. Record the
   exact app build and paired server revisions. No device pass is inferred from
   Node handlers or TypeScript checks.
5. Retain explicit incomplete/retry UX when the bounded operation cannot establish
   a current window. Confirm older continuation remains usable and inspect actual
   request counts and bytes before increasing the limits.

Source evidence: 95 executed behavior cases (61 foundation plus 34 consumer),
TypeScript 5.9.3, and the affected message, poll and failure-state checks passed.
Independent review exercised 13 consumer probes and the final neutral-access
retry changes. Removing cached-row replacement, poll request cancellation or the
stale-vote guard makes the corresponding regression fail. The foundation and
lookup merged in PR269 after all four required CI checks; their backend evidence
includes 45 lookup/history/contact/auth cases and independent ASGI/PostgreSQL
visibility checks. Eight extractor regressions cover query declarations in local
FastAPI dependencies; wrong query fields still fail the contract gate.

Final combined PR CI remains mandatory before merging the consumer slice. These
source checks do not substitute for the staged release and physical-device
acceptance above.

# Delivery outbox (c356)

Message send used to insert the ciphertext, commit, and only then loop the
recipient list calling the WS/push fan-out inline. A crash or Redis outage
between that commit and the loop — or an exception partway through it — lost a
recipient's delivery permanently, with no record it was ever owed and no retry.
`delivery_outbox` (migration 0039) and `app/services/outbox.py` close that gap
for messages. Board card c345 wired polls onto this same table and module; see
the `kind='poll'` section below and "Not covered" for what still does not exist.

Three states, defined precisely because they are easy to conflate:

- **STORED**: the delivery_outbox row committed in the same transaction as the
  domain write it backs (the Message insert). This is the durability guarantee —
  a stored row survives a crash and will eventually be retried.
- **DELIVERED**: `publish_to_user` returned without raising for that recipient.
  This is unchanged from before this card: an offline recipient with no open
  socket still counts as delivered, exactly like the existing documented
  behavior in `tests/test_ws_fanout.py`'s
  `test_message_published_while_recipient_offline_is_dropped_but_http_catchup_exists`.
  Catch-up for an offline recipient is still `GET
  /conversations/{id}/messages`, unchanged by this card.
- **READ**: the existing receipt upsert, `POST /messages/{id}/receipts`,
  untouched by this card.

## How legacy retry works

The following is the default behavior while deletion-aware delivery admission is
disabled. The separate opt-in protocol below changes uncertain-effect replay.

`send_message` enqueues the outbox row before its own commit, so the message and
the delivery intent commit or roll back together. After commit it attempts
delivery immediately (`outbox.dispatch_now`), reusing the same request session
for the short follow-up write — no second database connection is opened on the
request path. If that immediate attempt fails, is interrupted by a crash, or
the process dies before it runs at all, the row is left pending exactly as
enqueued.

A background sweeper (`outbox.dispatch_pending`, started in `app/main.py`'s
lifespan next to the existing Redis boot probe) picks up pending rows on a
timer. It never holds a database session, connection, or row lock across the
Redis publish attempt: chirp-ws runs with a database pool of size 1
(`DB_POOL_SIZE=1`, `DB_MAX_OVERFLOW=1` — `infra/deployment.json`,
`services.ws.env`) and the sweeper runs on every instance, so holding a
connection open for the length of a publish attempt would starve the
gateway's own periodic SQL. Instead it uses a lease: one short transaction
claims a batch with `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
LOCKED) RETURNING ...` (same shape as `app/services/prekey_service.py`'s
one-time-prekey handout), pushing `next_attempt_at` into the future and
bumping `attempts` as the claim itself, and — for a `message` row — reading
the ciphertext back from the `messages` table by `payload['message_id']`
inside that same transaction, the one place ciphertext is ever read back. The
transaction commits immediately, releasing the connection and the row lock.
The publish attempt then runs with no session at all, bounded by
`outbox_dispatch_timeout_s`. A second short transaction records the outcome:
full success deletes the row (decision: delivered rows are deleted, not kept);
partial or total failure narrows `recipient_ids` to the still-failing subset,
applies an exponential backoff to `next_attempt_at`, and sets a short
`last_error` code — never ciphertext, never raw exception text, since an
exception can carry a transport credential. Reaching `outbox_max_attempts`
sets `dead_at` instead of retrying again; dead rows are kept, not deleted, for
visibility, and log one warning naming only the row id.

E2EE v2 messages (board c444, `POST /v2/conversations/{id}/messages`) use the same
`message` rows and the same sweeper, with one difference in the rebuilt event: a v2
message has no message-level ciphertext (`messages.ciphertext` is NULL; the bytes are
per-device rows in `message_legs`), so its event carries `envelope_version` and no
`ciphertext` key at all. The event goes verbatim to every recipient user and each
device's leg is its own, so a receiving device fetches its leg over HTTP
(`GET /v2/conversations/{id}/messages?device_id=...`). `dispatch_pending` branches on
`messages.envelope_version`; v1 events are byte-identical to before.

A crash between the claim transaction and the outcome transaction leaves the
row exactly as the lease set it — attempts already bumped, `next_attempt_at`
in the future. Once that lease expires the row is claimed again: this is an
at-least-once retry, not an at-most-once guarantee. A message can in the rare
case be published twice (the live path still in flight against Redis when the
sweeper independently claims the same row after its `next_attempt_at`). This
is explicitly covered by the client's own dedupe on `message_id`
(`app-mobile/app/(tabs)/messages/[id].tsx`, around line 160), not by anything
on the server — no mobile change is needed or made here.

## Polls (`kind='poll'`, board card c345)

Board card c345's remainder wired `app/routers/polls.py` onto this same table
and module, following the exact same enqueue-before-commit /
`dispatch_now`-after-commit / sweeper-retries-on-failure shape as messages,
with three differences specific to polls. First, the payload is the aggregate
snapshot `_prepare_broadcast` already builds — counts only, never voter
identity, matching this file's own module docstring rule and `polls.py`'s
secret-ballot rule — so `dispatch_pending`'s `kind == 'poll'` branch uses the
payload directly with no hydration step and no `source_missing` dead-letter
path; there is no separate source table a poll row could outlive the way a
`message` row depends on the `messages` table. Second, `polls.py` coalesces at
enqueue time: before inserting a new row it deletes every other still-open
(`delivered_at IS NULL AND dead_at IS NULL`) pending `'poll'` row for the same
`poll_id`, unconditionally — not scoped to `next_attempt_at <= now()`, the
predicate that would look like the natural match for "still pending,
unleased." That scoped predicate was floated early on this card and is wrong:
`dispatch_now`'s own failure branch pushes a row's `next_attempt_at` into the
future synchronously inside the same request that failed, so a second write
minutes later would find the first row already looking "leased" and both
rows would survive uncoalesced. The correct predicate is delivered/dead-scoped
only, so at most one pending snapshot per poll ever exists, always the latest
tally. Third, `sender_id` is always `None` for polls — there is no per-
recipient content-free push for a poll to skip, unlike a message's sender.
`_broadcast`'s contract changed from returning `None` to returning the list of
recipient ids (as strings) that did not receive the event, matching the
`Dispatcher` shape `dispatch_now`/`dispatch_pending` call generically; it is
registered as `outbox.register_dispatcher('poll', _broadcast)` at
`polls.py` import time.

## Deletion-aware delivery (c436, disabled by default)

`ACCOUNT_DELETION_DELIVERY_ADMISSION_ENABLED` defaults to `false`. Enabling it
requires migration 0048 and separately reviewed runtime permissions. Both
`dispatch_now` and `dispatch_pending` then use `account_delivery_admission.py`.
Immediate dispatch requires a clean, committed request session and reconstructs
its inputs from committed rows; it does not trust caller-supplied recipients or
event bytes. Its short registry transactions use independent sessions.

The approved policy drops queued recipients whose deletion hold is active, whose
identity is tombstoned, or whose account is missing, and continues delivery to
others. A bounded preflight and sorted exclusive account locks precede the queue
row lock. Each claim revalidates at most 128 recipient IDs and registers at most
eight singleton-recipient attempts before transport work; each account has at
most 32 unresolved slots. Unstarted recipients remain queued and can advance on
later sweeps. Full-slot recipients are deferred with backoff and rotated behind
other recipients. The sweep selects at most 128 queue rows.

Each registration survives independently of source-row coalescing/deletion. Its
private owner token fences completion; a separate queue token fences ownership
of the remaining tail. Transport work holds no SQL connection. V1 ciphertext and
v2 content-free hints retain their existing wire formats. A confirmed success
removes that recipient's exact attempt even when another recipient times out.
Success means local transport acceptance; it does not prove device receipt.

Exceptions, failed-recipient results, invalid results and cancellation can follow
a real publish. These attempts remain running/uncertain and block account deletion
instead of being blindly replayed. A queue lease expiring does not clear evidence
or authorize another call for that recipient. Cancellation-resistant tasks remain
owned; later completion leaves a review blocker. Only unstarted queue entries
continue through normal sweeps. Resolving uncertain work requires a separately
reviewed procedure; there is no automatic expiry or recovery command.

Per-recipient terminal writes and queue finalization share one bounded transaction.
Claim fences prevent old workers from changing newer queue ownership, and attempt
fences prevent them from clearing another effect. Completed rows are removed
rather than retained as permanent delivery history. Legacy stats remain row counts;
`successful_recipients`, `suppressed` and `uncertain` are recipient counts.

Drain all old/flag-off API and worker dispatchers before relying on this registry
for deletion. WebSocket registration/drain is separately required for already
published frames. See `docs/C436-ROLLOUT.md` for rollout order, privileges and the
downgrade refusal when evidence remains. This source change activates no flag.

## Not covered

- Real push. `app/services/fcm_service.py` remains a log-only stub, unchanged.
- Delivery to a recipient who never reconnects. Catch-up is still `GET
  /conversations/{id}/messages` for messages, or the equivalent poll `GET`
  for polls; this card adds retry for the live fan-out path, not a new
  delivery mechanism.
- Delivery ordering. Neither messages nor polls guarantee two close-together
  events for the same conversation/poll arrive in write order — a sweep
  already in flight when a fresher event is enqueued can still land after it.
  For polls the mobile client applies each incoming snapshot directly with no
  sequence or version check, so an out-of-order delivery can briefly show a
  stale tally until the next read or event; this is a pre-existing
  best-effort property, not something c345 newly introduces or newly fixes.

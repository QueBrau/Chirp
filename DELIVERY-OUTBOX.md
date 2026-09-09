# Delivery outbox (c356)

Message send used to insert the ciphertext, commit, and only then loop the
recipient list calling the WS/push fan-out inline. A crash or Redis outage
between that commit and the loop — or an exception partway through it — lost a
recipient's delivery permanently, with no record it was ever owed and no retry.
`delivery_outbox` (migration 0039) and `app/services/outbox.py` close that gap
for messages. Polls are not wired into this table in this PR; see "Not covered"
below.

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

## How retry works

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

A crash between the claim transaction and the outcome transaction leaves the
row exactly as the lease set it — attempts already bumped, `next_attempt_at`
in the future. Once that lease expires the row is claimed again: this is an
at-least-once retry, not an at-most-once guarantee. A message can in the rare
case be published twice (the live path still in flight against Redis when the
sweeper independently claims the same row after its `next_attempt_at`). This
is explicitly covered by the client's own dedupe on `message_id`
(`app-mobile/app/(tabs)/messages/[id].tsx`, around line 160), not by anything
on the server — no mobile change is needed or made here.

## Not covered

- Real push. `app/services/fcm_service.py` remains a log-only stub, unchanged.
- Delivery to a recipient who never reconnects. Catch-up is still `GET
  /conversations/{id}/messages`; this card adds retry for the live fan-out
  path, not a new delivery mechanism.
- **Polls.** c345 already made poll writes commit before best-effort broadcast
  (`app/routers/polls.py`'s `_prepare_broadcast`/`_broadcast`), but that
  broadcast still has no durable record and no retry — a Redis outage during
  a poll write drops the event just as unrecoverably as before this card, it
  just fails quietly (one throttled warning log) instead of loudly. Wiring
  polls into this same outbox is a real, separate piece of work, deliberately
  left as a named follow-up rather than done in this PR: `_broadcast`
  currently returns nothing, and `tests/test_c345_poll_delivery.py` pins the
  literal `_broadcast` function object (its monkeypatches target
  `polls.publish_to_user` and `polls.POLL_BROADCAST_TIMEOUT_SECONDS` by name)
  as the thing that runs post-commit — a generic, kind-agnostic dispatcher
  living in `app/services/outbox.py` would not be intercepted by those
  monkeypatches and would break both tests. The follow-up needs to register a
  `'poll'` dispatcher in `app/services/outbox.py` and change `_broadcast`'s
  return contract from `None` to a delivered/failed accounting, then have the
  four poll write routes enqueue in-transaction and call `dispatch_now`
  instead of `_broadcast` directly. `polls.py` has zero references to
  `app.services.outbox` in this PR.

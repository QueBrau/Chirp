# Poll delivery and lifecycle

Poll writes build their response, aggregate update and active-recipient snapshot
inside one database transaction. They commit before publishing. Delivery receives
plain values and no database session, so a slow broker cannot retain a request's
connection or poll row lock. Failed commits publish nothing.

Vote, close and delete share a parent poll row lock. A vote that obtains the lock
first is recorded before a close, and prevents deletion. A close that wins rejects
later votes with `409 poll_closed`; a delete that wins leaves later votes with
`404 poll_not_found`. A poll with any ballot remains a record and returns
`409 poll_has_ballots` on deletion. Repeated close is still a silent no-op.

The whole recipient batch has one second for best-effort delivery. There is no
fresh timeout per recipient or detached background task. Slow or failed Redis
delivery preserves the successful HTTP write. One incomplete-batch warning reports
poll/chapter IDs, action, recipient/success/failure counts, timeout and elapsed time.
It excludes voter identity, choices, poll text, credentials and exception text.
The warning contains JSON within the existing application log message; this change
does not reconfigure Cloud Logging formatting, alerting or sinks.

Votes allow 30 attempts per minute per verified account/poll pair. This budget is
checked before database lookup through the shared rate limiter. Production uses
Redis; the existing per-process fallback during an outage remains a mitigation,
not a hard distributed limit. Five changes of mind fit comfortably below the cap.
The first request beyond the cap returns `429 poll_vote_rate_limited`.

New poll options are limited to 200 Unicode code points per option, with the
existing ten-option maximum. Oversized input returns 422 before storage. The mobile
option field uses its native `maxLength=200` guard; platform text counting can be
stricter for characters represented by UTF-16 surrogate pairs. Stored choices are
never silently truncated. Legacy oversized option text is unchanged and requires
an explicit operational review. Async cancellation bounds cooperative broker waits;
it is not a hard deadline for synchronous JSON serialization or arbitrary CPU work.

This was the immediate c345/c357 repair, now extended by the remainder of c345:
every poll write also enqueues a `delivery_outbox` row (kind='poll') in the same
transaction as the domain write, coalesced so at most one pending snapshot exists
per poll at a time, and the c356 sweeper now carries a real 'poll' dispatcher
branch. Database reads remain authoritative when a delivery is missed, but a
missed live delivery is no longer only a log line: it is a durable, at-least-once
retry with exponential backoff (dead-lettered after `outbox_max_attempts`), and
queue age is now visible via `outbox.queue_stats()`. This does not add real push,
offline catch-up beyond the existing `GET` re-read, or any delivery-ORDERING
guarantee across two close-together writes to the same poll: the client applies
each incoming snapshot directly with no sequence or version check, so a sweep
that was already in flight when a fresher event was enqueued can still land after
it and briefly show a stale tally until the next read or event. That race is
pre-existing and unchanged by this card, not newly introduced or newly fixed.
Recipient collection is still proportional to chapter membership. Local synthetic
chapter/broker tests establish transaction release, coalescing and correct
persisted tallies; they do not establish production traffic capacity or outage
recovery guarantees.

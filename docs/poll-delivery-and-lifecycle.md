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

This is the immediate c345/c357 repair. Database reads remain authoritative when a
delivery is missed. It does not add durable retries, aggregate coalescing, delivery
ordering, queue-lag monitoring or a guarantee that every member eventually receives
the final update. Those remain c356 and the outstanding c345 acceptance criteria.
Recipient collection is still proportional to chapter membership. Local synthetic
chapter/broker tests establish transaction release and correct persisted tallies;
they do not establish production traffic capacity or outage recovery guarantees.

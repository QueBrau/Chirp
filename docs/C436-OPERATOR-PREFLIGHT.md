# Account-deletion operator preflight

The account_deletion_preflight module is a read-only inspection of one deletion
request. It requires DATABASE_URL in the environment plus the canonical
--request-id and --expected-user-id UUID arguments:

    DATABASE_URL=postgresql+asyncpg://operator@localhost/chirp \
      python -m app.jobs.account_deletion_preflight \
      --request-id REQUEST_UUID \
      --expected-user-id USER_UUID

The command uses a PostgreSQL read-only transaction with bounded statement,
lock, idle-transaction, connection, and overall timeouts. It rolls the
transaction back and closes the engine on every path. It binds the request to
the expected user and deletion kind, reports the request and journal statuses,
detects active or malformed leases and Firebase unknown-outcome markers, and
validates succeeded provider receipts without printing their subject or scope
values.

Exit status 0 means the request was found and inspected, including when it is
blocked or pending. Exit status 1 means the request was not found for the
expected owner. Exit status 2 means invalid arguments or an inspection failure.
The JSON report always has ready_for_deletion false: this command does not prove
provider ownership, account quiescence, external readback, operator authority,
or retention policy. It imports no provider adapter and cannot invoke deletion.

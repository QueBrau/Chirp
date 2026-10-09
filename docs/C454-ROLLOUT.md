# c454 export privilege preflight

Run the maintenance command from the backend environment with the database URL
pointing at the intended API database:

```bash
python -m app.jobs.account_data --preflight-export-privileges \
  --expected-role chirp_api --timeout-seconds 30
```

The command starts a read-only transaction, verifies the live database identity,
checks `SELECT` on every ORM column used by the account export, and executes a
zero-row `SELECT` for each relation. It also checks the `SELECT`/`INSERT`/`UPDATE`
table privileges required by the account-data request and artifact routes. The
read checks use column privileges; request/artifact `INSERT` and `UPDATE` checks
use table privileges because those routes write rows. Column grants are accepted,
so a least-privilege grant such as
`chapter_stripe_customers(created_at)` is reported correctly without requiring a
table-wide `SELECT` grant.

The command exits nonzero on an identity mismatch, missing column or write
privilege, missing relation, or failed query. It never grants privileges, changes
rows, creates roles, or runs export/deletion work. `--delete` cannot be combined
with the preflight option. This check proves the current connection's ACL and
schema surface only; it does not validate application authorization, provider
credentials, or cloud deployment routing.

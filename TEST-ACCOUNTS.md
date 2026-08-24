# Test accounts — one per user type (board card c159)

Twelve seeded accounts for looking at what each kind of user actually sees. Eight
roles exist in the backend and, before this, six of them had never been seen
rendered: the app is hardwired to the `chirps-prod` Firebase project, so viewing
the treasurer dashboard meant *being* a real treasurer of a real chapter in
production.

**There are no passwords here, anywhere.** These accounts only exist in a local
database and only work under `AUTH_MODE=emulated`, where the backend trusts an
`X-Debug-Firebase-Uid` header instead of Firebase. Nothing in this file is a
credential, and none of it reaches production — see [Why this cannot touch
prod](#why-this-cannot-touch-prod).

## Start it

Three commands, then a browser.

```bash
docker run -d --name chirp-dev-pg -e POSTGRES_USER=chirp -e POSTGRES_PASSWORD=chirp -e POSTGRES_DB=chirp -p 5440:5432 postgres:16
```

```bash
cd backend && DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5440/chirp' .venv/bin/alembic upgrade head
```

```bash
cd backend && DATABASE_URL='postgresql+asyncpg://chirp:chirp@localhost:5440/chirp' .venv/bin/python scripts/seed_dev_accounts.py
```

Then start both servers from `.claude/launch.json` — **`chirp-api-local`** (port
8000) and **`chirp-web-local`** (port 8081). `chirp-web-local` is the one that
points the app at your local API; plain `chirp-web` still points at production.

## The accounts

Open any of these and you are that person. Switching account is a page load, not
a live toggle, so no screen is ever left holding the previous account's data.

| Who | URL | Role | Org |
|---|---|---|---|
| Marcus Webb | http://localhost:8081/?uid=dev-president | president | Sigma Chi |
| Andre Coleman | http://localhost:8081/?uid=dev-vice-president | vice_president | Sigma Chi |
| Priya Raman | http://localhost:8081/?uid=dev-treasurer | treasurer | Sigma Chi |
| Jordan Ellis | http://localhost:8081/?uid=dev-secretary | secretary | Sigma Chi |
| Sam Okafor | http://localhost:8081/?uid=dev-historian | historian | Sigma Chi |
| Chris Delgado | http://localhost:8081/?uid=dev-member | member | Sigma Chi |
| Tyler Nguyen | http://localhost:8081/?uid=dev-pledge | pledge | Sigma Chi |
| Ray Whitfield | http://localhost:8081/?uid=dev-alumni | alumni | Sigma Chi |
| Naomi Frazier | http://localhost:8081/?uid=dev-sorority-president | president | **Alpha Delta Pi** |
| Dana Brooks | http://localhost:8081/?uid=dev-campus-student | — | **no org** |
| Alex Moreno | http://localhost:8081/?uid=dev-unverified | — | no org, **no .edu** |
| Platform Admin | http://localhost:8081/?uid=dev-admin | — | **platform admin** |

**The choice sticks.** Once you open one of those URLs, a bare route like
`/feed` or `/chapter/treasurer` keeps the same account — you do not have to carry
`?uid=` around, and editing the path will not silently drop you at the sign-in
screen. The account you are currently wearing is shown in the browser tab title
(`dev-treasurer · Chirp (dev)`), because a sticky session you cannot see is its
own kind of confusion.

To switch, put a different `?uid=` in the URL. To stop impersonating and get the
real sign-in screen back, use **`?uid=off`**.

On a phone build there is no URL bar, so it comes from the environment instead:
`EXPO_PUBLIC_DEV_UID=dev-treasurer`.

## The four that show you something you cannot otherwise see

- **`dev-treasurer`** — the only account with money behind it. 15 ledger entries
  across 7 spend categories, a dues cycle with 5 of 8 members paid, and two spend
  approvals (one pending, one decided), so the c118 charts have real shape rather
  than a single flat line. Go to `/chapter/treasurer`.
- **`dev-sorority-president`** — a *second* chapter, so org colour scoping
  (DESIGN §8.6) is provable rather than assumed: her entire Orgs stack renders in
  Alpha Delta Pi azure where Sigma Chi's is blue. Same screens, different colours.
- **`dev-unverified`** — has a campus but no `.edu` verification, which is the c88
  gate state. Campus feed and Yak refuse her and c90's "Verify my .edu" screen is
  the destination. **This state was previously unreachable without deliberately
  breaking a working account.**
- **`dev-campus-student`** — belongs to no org at all, which the Aug 11 product
  decision says is a first-class Chirp user (c71). Campus surfaces work; the Orgs
  tab shows the find-your-org state.

## Where to look

`/` Home · `/yak` Yak · `/messages` Messages · `/chapter` Orgs · `/profile` Profile

Role-gated screens under Orgs → Tools: `/chapter/treasurer`,
`/chapter/secretary`, `/chapter/president`, `/chapter/historian`,
`/chapter/members`, `/chapter/moderation`, `/chapter/tree`, `/chapter/dues`.

Deep links take the uid too, so you can jump straight in:
`http://localhost:8081/chapter/treasurer?uid=dev-treasurer`.

## Why this cannot touch prod

Three independent locks, none of which depends on anybody remembering to remove
a switch before shipping:

1. **`__DEV__`** is false in any release build, so `devAuthUid()` returns null and
   the header is never set.
2. **The server ignores `X-Debug-Firebase-Uid`** entirely unless `AUTH_MODE` is
   `emulated`.
3. **The production ENV guard** (SECURITY-REVIEW finding 5) refuses to boot with
   anything but `AUTH_MODE=firebase`, so lock 2 can never be satisfied in prod.

The seed script adds a fourth for itself: it **refuses to run** against a
`DATABASE_URL` that is not localhost, and refuses a Cloud SQL socket URL outright.
Both refusals are worth seeing once — point it at a fake prod URL and it exits
before opening a connection.

Deliberately **not** done with real Firebase accounts: that means creating accounts
and handling passwords, it seeds invented people into production (exactly what
c97/c99/c100 spent a session removing), and since c88 a fake account still could
not see campus content without a real `.edu` — so most of the dashboards would
have been wrong anyway.

## What these accounts do not cover

**Realtime.** An impersonated session has no bearer token, and a browser cannot
put a header on a WebSocket — the gateway authenticates through the subprotocol,
which reads that token. So the socket is deliberately not connected under a
`?uid=` session rather than attempting a connection that can only fail and
printing a console error on every page load. Messages will render; live delivery
is not part of what this covers.

**Switching branches under a running dev server.** This is all on `main` now, so
the everyday case is fine. But it is worth knowing why it fails when it fails:
if you check out a branch that does not have `src/auth/devAuth.ts`, Metro keeps
serving a bundle that references a module no longer on disk and the app goes to a
blank black screen with nothing useful in the console. Check the branch back out
and reload — nothing is broken.

## Re-seeding

The script is idempotent, so re-running it after a migration is safe and updates
rows in place.

The ledger is the exception, and on purpose: `ledger_entries` carries a Postgres
trigger (`ledger_append_only`) that raises on UPDATE and DELETE, so seeded finance
data **cannot** be cleared in place — that guard is doing its job. To get a clean
set of finance data, drop the database and start over:

```bash
docker exec chirp-dev-pg psql -U chirp -d postgres -c "DROP DATABASE chirp WITH (FORCE);" -c "CREATE DATABASE chirp OWNER chirp;"
```

Then re-run the migrate and seed commands above.

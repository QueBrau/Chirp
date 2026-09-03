# Chirp (Frat App) — Full Technical Spec & Build Plan

A mobile-first social + operations platform for fraternity/sorority chapters: E2EE messaging, chapter feed, anonymous campus board, big/little lineage trees, dues collection, and role-based dashboards.

---

## 1. Stack

| Layer | Choice | Notes |
|---|---|---|
| Mobile app | Expo (React Native), iOS + Android | **Dev build required** (`expo prebuild` / EAS Build), NOT Expo Go — libsignal needs native modules |
| Navigation | Expo Router, tab-based | Tabs: Feed, Chirps, Messages, Chapter, Profile. Chapter tab contents are role-gated |
| Backend | FastAPI (Python 3.12) on Cloud Run | Stateless, containerized, scales to zero |
| Database | Cloud SQL — PostgreSQL 16 | Connect via Cloud SQL connector/unix socket; use connection pooling (SQLAlchemy pool limits) |
| Realtime | WebSockets on Cloud Run + Memorystore (Redis) pub/sub | Redis is transport/fan-out ONLY — never message storage |
| Push | FCM via Expo Notifications | Content-free notifications ("New message from Maria"), deep-link into thread |
| Auth | Firebase Auth | Email/password + Google + Apple Sign-In (Apple required by App Store if any social login exists). Backend verifies Firebase JWT on every request |
| Payments | Stripe Connect + Stripe React Native SDK | Each chapter = connected account. PaymentSheet with Apple Pay / Google Pay. Card data never touches our servers |
| E2EE crypto | libsignal (`@signalapp/libsignal-client`) | Signal protocol: X3DH + Double Ratchet for 1:1, sender keys for groups. Keys in iOS Keychain / Android Keystore |
| Local storage | expo-sqlite | On-device message store (decrypted history lives ONLY here), offline queue, local message search |
| Graph rendering | react-native-skia + d3-hierarchy + gesture-handler + reanimated | d3-hierarchy computes layout math only; Skia renders; gestures for pan/pinch/tap |
| Secrets | GCP Secret Manager | No plaintext secrets in env/code |
| Field encryption | Google Tink + Cloud KMS envelope encryption | For high-sensitivity columns only (bank/tax info if ever stored) |

---

## 2. Architecture Principles

1. **Server never reads message content.** Messages are ciphertext blobs. No server-side message search, no content-based features, no message bodies in logs or push payloads. Ever.
2. **Money lives in the dashboard, talk lives in chat.** All financial records are server-side, auditable, append-only. Chat is private/E2EE. This is an explicit product rule.
3. **Org scoping is middleware, not per-endpoint discipline.** Every request resolves `(user_id, chapter_id) → role` via the memberships table before handlers run. One missed check = one chapter reading another's data.
4. **One auth system, three experiences.** Greek members, non-greek users, and alumni are the same Firebase user type differentiated by membership records — not separate auth flows.
5. **Append-only ledger.** Never UPDATE or DELETE a financial row. Corrections are new offsetting entries.
6. **Anonymous to peers, pseudonymous to the server.** Chirps posts hide identity from users but retain author_id server-side for moderation/bans.

---

## 3. Postgres Schema (v1)

```sql
-- ============ IDENTITY & ORGS ============

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    firebase_uid    TEXT UNIQUE NOT NULL,
    email           TEXT UNIQUE NOT NULL,
    display_name    TEXT NOT NULL,
    avatar_url      TEXT,
    account_type    TEXT NOT NULL CHECK (account_type IN ('greek', 'non_greek', 'alumni')),
    campus_id       UUID REFERENCES campuses(id),
    is_ghost        BOOLEAN NOT NULL DEFAULT FALSE, -- placeholder members for historical lineage
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE campuses (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name    TEXT NOT NULL,
    slug    TEXT UNIQUE NOT NULL
);

CREATE TABLE chapters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campus_id       UUID NOT NULL REFERENCES campuses(id),
    org_name        TEXT NOT NULL,        -- e.g. "Sigma Chi"
    chapter_name    TEXT,                 -- e.g. "Epsilon Mu"
    stripe_account_id TEXT,               -- Stripe Connect connected account
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id),
    chapter_id  UUID NOT NULL REFERENCES chapters(id),
    role        TEXT NOT NULL CHECK (role IN
                ('president','vice_president','treasurer','secretary',
                 'historian','member','pledge','alumni')),
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','removed')),
    pledge_class TEXT,                    -- e.g. "Fall 2024"
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, chapter_id)
);
CREATE INDEX idx_memberships_chapter ON memberships(chapter_id) WHERE status = 'active';
CREATE INDEX idx_memberships_user ON memberships(user_id) WHERE status = 'active';

CREATE TABLE chapter_invites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id  UUID NOT NULL REFERENCES chapters(id),
    code        TEXT UNIQUE NOT NULL,     -- deep-link invite code
    role        TEXT NOT NULL DEFAULT 'member',
    expires_at  TIMESTAMPTZ,
    created_by  UUID NOT NULL REFERENCES users(id)
);

-- ============ E2EE KEY DIRECTORY ============

CREATE TABLE devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    device_label    TEXT,
    registration_id INTEGER NOT NULL,     -- libsignal registration id
    identity_key    BYTEA NOT NULL,       -- public identity key
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

CREATE TABLE signed_prekeys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   UUID NOT NULL REFERENCES devices(id),
    key_id      INTEGER NOT NULL,
    public_key  BYTEA NOT NULL,
    signature   BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE one_time_prekeys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   UUID NOT NULL REFERENCES devices(id),
    key_id      INTEGER NOT NULL,
    public_key  BYTEA NOT NULL,
    consumed_at TIMESTAMPTZ               -- server hands out once, marks consumed
);
CREATE INDEX idx_otk_available ON one_time_prekeys(device_id) WHERE consumed_at IS NULL;

-- PQXDH: one signed last-resort Kyber prekey per device (is_last_resort = TRUE, never
-- consumed) plus an optional one-time Kyber pool (consumed like one_time_prekeys above).
CREATE TABLE kyber_prekeys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id       UUID NOT NULL REFERENCES devices(id),
    key_id          INTEGER NOT NULL,
    public_key      BYTEA NOT NULL,
    signature       BYTEA NOT NULL,        -- signed by the device identity key
    is_last_resort  BOOLEAN NOT NULL DEFAULT FALSE,
    consumed_at     TIMESTAMPTZ,           -- one-time kybers only; last-resort never consumed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_kyber_otk_available ON kyber_prekeys(device_id)
    WHERE consumed_at IS NULL AND NOT is_last_resort;

-- ============ MESSAGING (ciphertext only) ============

CREATE TABLE conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id  UUID REFERENCES chapters(id),  -- NULL for cross-chapter DMs
    kind        TEXT NOT NULL CHECK (kind IN ('dm','group')),
    title       TEXT,                          -- group name (plaintext metadata, OK)
    protocol_version INTEGER NOT NULL DEFAULT 2, -- 2 = E2EE from day one
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversation_members (
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    user_id         UUID NOT NULL REFERENCES users(id),
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at         TIMESTAMPTZ,               -- triggers sender-key rotation client-side
    PRIMARY KEY (conversation_id, user_id)
);

CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    sender_device_id UUID NOT NULL REFERENCES devices(id),
    ciphertext      BYTEA NOT NULL,            -- server NEVER parses this
    message_type    TEXT NOT NULL DEFAULT 'signal', -- signal | sender_key_distribution
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_convo_time ON messages(conversation_id, created_at DESC);

CREATE TABLE message_receipts (
    message_id  UUID NOT NULL REFERENCES messages(id),
    device_id   UUID NOT NULL REFERENCES devices(id),
    delivered_at TIMESTAMPTZ,
    PRIMARY KEY (message_id, device_id)
);

-- ============ SOCIAL FEED ============

CREATE TABLE posts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id  UUID NOT NULL REFERENCES chapters(id),
    author_id   UUID NOT NULL REFERENCES users(id),
    body        TEXT NOT NULL,
    media_urls  TEXT[],
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);
CREATE INDEX idx_posts_chapter_time ON posts(chapter_id, created_at DESC);

CREATE TABLE post_likes (
    post_id UUID NOT NULL REFERENCES posts(id),
    user_id UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_id, user_id)
);

CREATE TABLE post_comments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id     UUID NOT NULL REFERENCES posts(id),
    author_id   UUID NOT NULL REFERENCES users(id),
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);

-- ============ CHIRP (anonymous board) ============

CREATE TABLE chirps (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campus_id   UUID NOT NULL REFERENCES campuses(id),
    author_id   UUID NOT NULL REFERENCES users(id),  -- NEVER exposed via API
    body        TEXT NOT NULL,
    score       INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at  TIMESTAMPTZ,
    removed_reason TEXT
);
CREATE INDEX idx_chirps_campus_time ON chirps(campus_id, created_at DESC) WHERE removed_at IS NULL;

CREATE TABLE chirp_votes (
    chirp_id  UUID NOT NULL REFERENCES chirps(id),
    user_id UUID NOT NULL REFERENCES users(id),
    value   SMALLINT NOT NULL CHECK (value IN (-1, 1)),
    PRIMARY KEY (chirp_id, user_id)
);

CREATE TABLE content_reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporter_id     UUID NOT NULL REFERENCES users(id),
    target_type     TEXT NOT NULL CHECK (target_type IN ('chirp','post','comment','message_forward','user')),
    target_id       UUID,
    forwarded_plaintext TEXT,      -- for E2EE message reports: client forwards plaintext
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','actioned','dismissed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_blocks (
    blocker_id UUID NOT NULL REFERENCES users(id),
    blocked_id UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- c279: WHY the block was made, because it decides WHAT it hides. 'named' hides
    -- everything; 'by_chirp' hides chirp surfaces only, so blocking an anonymous
    -- author cannot be detected by diffing the named feed before and after (which
    -- would name them, defeating principle 6). Contact refusal enforces both.
    source     TEXT NOT NULL DEFAULT 'named' CHECK (source IN ('named','by_chirp')),
    PRIMARY KEY (blocker_id, blocked_id)
);

-- ============ LINEAGE (family tree) ============

CREATE TABLE families (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id  UUID NOT NULL REFERENCES chapters(id),
    name        TEXT NOT NULL,      -- "Hammer family"
    color       TEXT NOT NULL DEFAULT '#6366f1'
);

CREATE TABLE lineage_edges (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id      UUID NOT NULL REFERENCES chapters(id),
    big_user_id     UUID NOT NULL REFERENCES users(id),
    little_user_id  UUID NOT NULL REFERENCES users(id),
    family_id       UUID REFERENCES families(id),
    pledge_class    TEXT,
    confirmed_by_little BOOLEAN NOT NULL DEFAULT FALSE,
    created_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (little_user_id, chapter_id)  -- one big per member per chapter
);
CREATE INDEX idx_lineage_chapter ON lineage_edges(chapter_id);

-- ============ FINANCE (append-only) ============

CREATE TABLE dues_cycles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id  UUID NOT NULL REFERENCES chapters(id),
    name        TEXT NOT NULL,          -- "Spring 2027 Dues"
    amount_cents INTEGER NOT NULL,
    due_date    DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ledger_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id      UUID NOT NULL REFERENCES chapters(id),
    entry_type      TEXT NOT NULL CHECK (entry_type IN
                    ('dues_payment','expense','budget_allocation','correction','payout')),
    amount_cents    INTEGER NOT NULL,   -- positive = in, negative = out
    category        TEXT,
    description     TEXT,
    related_user_id UUID REFERENCES users(id),   -- who paid, for dues
    dues_cycle_id   UUID REFERENCES dues_cycles(id),
    stripe_payment_intent_id TEXT,
    corrects_entry_id UUID REFERENCES ledger_entries(id), -- for corrections
    created_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    -- NO updated_at. NO soft delete. Append-only by design.
);
CREATE INDEX idx_ledger_chapter_time ON ledger_entries(chapter_id, created_at DESC);

CREATE TABLE spend_approvals (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id  UUID NOT NULL REFERENCES chapters(id),
    requested_by UUID NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    decided_by  UUID REFERENCES users(id),
    decided_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ SECRETARY ============

CREATE TABLE meetings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chapter_id  UUID NOT NULL REFERENCES chapters(id),
    title       TEXT NOT NULL,
    meeting_date TIMESTAMPTZ NOT NULL,
    minutes_md  TEXT,                   -- markdown minutes
    created_by  UUID NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE meeting_attendance (
    meeting_id UUID NOT NULL REFERENCES meetings(id),
    user_id    UUID NOT NULL REFERENCES users(id),
    status     TEXT NOT NULL CHECK (status IN ('present','absent','excused')),
    PRIMARY KEY (meeting_id, user_id)
);

-- ============ ALUMNI / JOBS ============

CREATE TABLE alumni_profiles (
    user_id     UUID PRIMARY KEY REFERENCES users(id),
    grad_year   INTEGER,
    company     TEXT,
    title       TEXT,
    industry    TEXT,
    linkedin_url TEXT,
    open_to_mentoring BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE job_posts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    posted_by   UUID NOT NULL REFERENCES users(id),
    chapter_id  UUID REFERENCES chapters(id),  -- NULL = network-wide
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    description TEXT NOT NULL,
    apply_url   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);
```

---

## 4. Backend Layout (FastAPI)

```
backend/
├── Dockerfile
├── pyproject.toml
├── alembic/                      # migrations
├── app/
│   ├── main.py                   # FastAPI app, middleware registration
│   ├── config.py                 # settings from env / Secret Manager
│   ├── db.py                     # SQLAlchemy async engine + session, pool limits
│   ├── middleware/
│   │   ├── auth.py               # Firebase JWT verification → request.state.user
│   │   └── org_scope.py          # resolves (user, chapter_id) → role; 403 if no membership
│   ├── models/                   # SQLAlchemy models mirroring schema above
│   ├── routers/
│   │   ├── auth.py               # register device, account bootstrap
│   │   ├── chapters.py           # CRUD, invites, member management
│   │   ├── keys.py               # key directory: upload bundles, fetch prekey bundles
│   │   ├── messages.py           # send ciphertext, fetch history, receipts
│   │   ├── feed.py               # posts, likes, comments
│   │   ├── chirps.py               # anonymous board + voting
│   │   ├── moderation.py         # reports, blocks, admin actions
│   │   ├── lineage.py            # families, edges, tree fetch (returns full adjacency for chapter)
│   │   ├── finance.py            # dues cycles, ledger (append-only enforced here too), approvals
│   │   ├── meetings.py           # minutes + attendance
│   │   ├── alumni.py             # profiles + job board
│   │   └── payments.py           # Stripe: onboarding links, payment intents, webhooks
│   ├── ws/
│   │   ├── gateway.py            # WebSocket endpoint, auth handshake, connection registry
│   │   └── pubsub.py             # Redis pub/sub bridge (channel per user_id)
│   ├── services/
│   │   ├── stripe_service.py
│   │   ├── fcm_service.py        # content-free push
│   │   └── prekey_service.py     # atomic one-time-prekey handout (consume-on-read)
│   └── core/
│       ├── permissions.py        # role constants + require_role() dependency
│       └── errors.py
└── tests/
    ├── test_org_scoping.py       # cross-chapter access MUST 403
    ├── test_ledger_append_only.py
    ├── test_prekey_consumption.py
    └── test_group_membership_leave.py  # left_at set → client rotation contract
```

**Key middleware contract:** every route under `/chapters/{chapter_id}/...` passes through `org_scope` which loads the caller's active membership or 403s. Role-gated routes use `require_role('treasurer', 'president')` dependencies. No handler does its own ad-hoc scoping.

**API surface (v1 sketch):**

- `POST /devices` — register device + upload identity key, signed prekey, batch of one-time prekeys
- `GET /users/{id}/prekey-bundle` — fetch bundle to start a session (consumes one OTK atomically)
- `POST /conversations` / `POST /conversations/{id}/messages` — ciphertext in, fan-out via Redis
- `GET /conversations/{id}/messages?before=` — ciphertext history pagination
- `GET /chapters/{id}/lineage` — full nodes + edges + families for tree render
- `POST /chapters/{id}/dues-cycles`, `POST /payments/dues/{cycle_id}/intent`, `POST /webhooks/stripe`
- `GET /chapters/{id}/ledger?category=&from=&to=` — treasurer views
- Standard CRUD for feed, chirps, meetings, alumni, jobs

---

## 5. Mobile App Layout (Expo)

```
app-mobile/
├── app.json / eas.json           # dev build config (NOT Expo Go)
├── app/                          # Expo Router
│   ├── (auth)/
│   │   ├── sign-in.tsx           # Apple / Google / email via Firebase
│   │   ├── account-type.tsx      # greek / non-greek / alumni path
│   │   └── join-chapter.tsx      # invite-code deep link landing
│   ├── (tabs)/
│   │   ├── feed/                 # For You (v1 = reverse-chron chapter posts)
│   │   ├── chirp/                  # campus anonymous board
│   │   ├── messages/             # conversation list, thread view
│   │   ├── chapter/              # role-gated: tree, treasurer, secretary, members
│   │   │   ├── tree.tsx          # Skia lineage graph
│   │   │   ├── treasurer.tsx     # visible if role in (treasurer, president)
│   │   │   ├── secretary.tsx     # visible if role in (secretary, president)
│   │   │   └── members.tsx
│   │   └── profile/              # own profile, alumni profile, settings
│   └── _layout.tsx
├── src/
│   ├── crypto/
│   │   ├── signal.ts             # libsignal wrapper: session mgmt, encrypt/decrypt
│   │   ├── keys.ts               # keygen, Keychain/Keystore storage, prekey replenishment
│   │   └── groups.ts             # sender key create/distribute/rotate-on-leave
│   ├── db/
│   │   ├── schema.ts             # expo-sqlite: local decrypted messages, outbox queue
│   │   └── search.ts             # local message search (server can't)
│   ├── realtime/
│   │   ├── socket.ts             # WS connect, auth, reconnect w/ backoff
│   │   └── queue.ts              # offline outbox → flush on reconnect
│   ├── api/                      # typed client for backend routes
│   ├── tree/
│   │   ├── layout.ts             # d3-hierarchy layout computation
│   │   └── TreeCanvas.tsx        # Skia render + gesture-handler pan/pinch/tap
│   ├── payments/
│   │   └── dues.tsx              # Stripe PaymentSheet flow
│   └── notifications/
│       └── push.ts               # FCM registration, content-free handling, deep links
```

---

## 6. E2EE Flow Reference (for implementation)

1. **Registration:** app generates identity keypair + registration id on device → private keys into Keychain/Keystore → `POST /devices` with public identity key, signed prekey, ~100 one-time prekeys. Replenish OTKs when server reports low.
2. **Start DM:** fetch recipient's prekey bundle per device → X3DH → Double Ratchet session → encrypt → `POST message` (ciphertext).
3. **Groups:** creator generates sender key → distributes via pairwise encrypted `sender_key_distribution` messages to each member device → group messages encrypted once with sender key.
4. **Member leaves/kicked:** server sets `left_at` → remaining clients rotate sender key and redistribute. **This is the most security-critical client behavior — test it first.**
5. **Receive:** ciphertext via WS (or history fetch) → decrypt on device → store plaintext in local SQLite only.
   **Client wire-format contract (spike-verified, Aug 2026):** the server stores both
   1:1 wire formats as `message_type='signal'` and does NOT distinguish
   PreKeySignalMessage (first message of a session) from SignalMessage. Receivers
   MUST try the PreKeySignalMessage parse first and fall back to SignalMessage —
   the two protobufs fail loudly when cross-parsed, so the fallback is safe.
   See `spikes/libsignal-node/FINDINGS.md` Finding 2.
6. **New phone (v1 policy):** fresh history, like Signal classic. Encrypted backups = post-launch fast-follow.
7. **Abuse reports on messages:** reporter's client forwards plaintext of reported messages into `content_reports.forwarded_plaintext` (standard E2EE-compatible pattern).

---

## 7. Milestones (build order)

| # | Milestone | Depends on | Notes |
|---|---|---|---|
| 1 | Expo dev-build skeleton + Firebase Auth + account types + join-via-invite deep links | — | Apple Sign-In required alongside Google |
| 2 | Chapters + IAM roles + org-scope middleware + role-gated tabs | 1 | Roles: president, VP, treasurer, secretary, **historian**, member, pledge, alumni |
| 3 | **libsignal RN spike** — encrypt/decrypt between two physical devices | 1 | De-risks the scariest unknown. Real devices, not simulators (Keychain/Keystore differ). If RN+libsignal fails, decide fallback NOW |
| 4 | E2EE DMs → group chats + sender-key rotation + local SQLite + offline queue + WS/Redis fan-out + content-free push | 2, 3 | "Messaging works" and "E2EE" are ONE milestone |
| 5 | Feed (reverse-chron v1) + Chirps **with moderation built in** (report, block, admin removal) | 2 | App Store Guideline 1.2 requires UGC moderation before submission |
| 6 | Family tree — lineage CRUD + Skia/d3 interactive graph, ghost nodes, family colors, little-confirms-big | 2 | Demo weapon. Historian/e-board edit rights |
| 7 | **TestFlight with a real chapter** | 4, 5, 6 | Validate social hook before building money machinery |
| 8 | Stripe Connect onboarding + dues PaymentSheet (Apple Pay / Google Pay) | 2 | Dues = physical-world service, no Apple 30% cut |
| 9 | Treasurer dashboard (dues cycles, append-only ledger, spend approvals, export) + Secretary dashboard (minutes, attendance) | 8 | The reason chapters adopt |
| 10 | Alumni network + job board; tree nodes link to alumni profiles | 6 | Tree ↔ alumni signup flywheel |
| 11 | Encrypted backups / new-phone history recovery | 4 | Passphrase-derived key, blob in Cloud Storage |

---

## 8. Non-Negotiable Rules for the Codebase

1. `messages.ciphertext` is never parsed, logged, indexed, or included in push payloads.
2. `ledger_entries` has no UPDATE/DELETE path anywhere — corrections reference `corrects_entry_id`.
3. Chirps API responses never include `author_id`.
4. Every `/chapters/{id}/*` route goes through org-scope middleware. Write the cross-chapter 403 test before the features.
5. Private keys never leave the device. No key material in Postgres except public keys.
6. Secrets via Secret Manager; logs redact tokens, emails where feasible, and all financial fields.
7. Stripe card data handled only by Stripe SDK — never transits our backend.

---

## 9. Scaffolding Instructions

Build in this order:

1. **Monorepo:** `backend/` (FastAPI per §4) + `app-mobile/` (Expo per §5). Docker compose for local Postgres 16 + Redis.
2. **Backend first:** Alembic migration implementing the full §3 schema. Firebase JWT middleware (stub verification behind an env flag for local dev). Org-scope middleware + `require_role` dependency. Stub all routers in §4 with typed request/response models — key directory and messages routes fully functional (they're just storage + fan-out), business routers can return placeholder data initially.
3. **WS gateway:** authenticated WebSocket endpoint + Redis pub/sub bridge, channel-per-user fan-out.
4. **Mobile skeleton:** Expo Router tab structure per §5, Firebase Auth screens, dev-build config with placeholders for libsignal native module setup.
5. **Tests from §8 rules** — especially cross-chapter 403 and ledger append-only.

Do NOT scaffold: Stripe webhook logic beyond a stub, the Skia tree canvas, or encrypted backups — those come in their milestone order.

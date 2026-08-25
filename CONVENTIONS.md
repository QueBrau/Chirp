# Chirp — Build Conventions

Read this AND `SPEC.md` before writing any code. SPEC.md is authoritative for the schema (§3),
backend layout (§4), mobile layout (§5), non-negotiable rules (§8), and scaffold scope (§9).
This file pins the naming/dependency contract so independently-built modules fit together.

**Product name:** Chirp. Monorepo: `backend/` (FastAPI) + `app-mobile/` (Expo).

---

## Backend

- Python: target 3.12 in Docker, but code MUST run on 3.11 (`requires-python = ">=3.11"`,
  no 3.12-only syntax) — local dev machine has 3.11.
- FastAPI + SQLAlchemy 2.0 async (`asyncpg`) + Alembic + Pydantic v2 + pydantic-settings.
- All Python files: full type hints, one-line module docstring, no wildcard imports, thin handlers.

### Core contract (everything imports these — signatures are FROZEN)

- `app.config`: `class Settings(BaseSettings)` with fields:
  `database_url: str = "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp"`,
  `redis_url: str = "redis://localhost:6379/0"`,
  `auth_mode: Literal["emulated", "firebase"] = "emulated"`,
  `firebase_project_id: str | None = None`,
  `stripe_secret_key: str | None = None`, `stripe_webhook_secret: str | None = None`,
  `cors_origins: list[str] = ["*"]`.
  Loads from env / `.env`. Expose `get_settings()` (lru_cache'd).
- `app.db`: `class Base(DeclarativeBase)`; **lazy** engine via `get_engine()` (created on first
  call, NOT at import — importing `app.main` must never touch the network); `get_session()`
  FastAPI dependency yielding `AsyncSession` (autocommit off, expire_on_commit=False).
- `app.middleware.auth`: FastAPI dependency `get_current_user` → returns `models.User`, 401 on failure.
  - emulated mode: trusts header `X-Debug-Firebase-Uid`, looks up `users.firebase_uid`.
  - firebase mode: verifies `Authorization: Bearer <id-token>` via `firebase_admin` (imported
    inside the function so the dep is optional locally).
  - `POST /auth/bootstrap` is the only route that accepts an authenticated-but-unregistered
    identity (creates the `users` row).
- `app.middleware.org_scope`: dependency `get_current_membership` — reads `chapter_id` path
  param + current user, returns active `models.Membership`, raises 403 `{"detail": "not_a_member"}`
  otherwise. EVERY `/chapters/{chapter_id}/...` route depends on it (§8.4).
- `app.core.permissions`: `class Role(str, Enum)` — president, vice_president, treasurer,
  secretary, historian, member, pledge, alumni. `EBOARD: frozenset[Role]` = first five.
  `require_role(*roles: Role)` — dependency factory layered on `get_current_membership`,
  403 `{"detail": "insufficient_role"}`.
- `app.core.errors`: shared HTTPException helpers (`not_found()`, `forbidden(detail)`, `conflict(detail)`).
- `app.ws.pubsub`: `async def publish_to_user(user_id: str, event: dict) -> None` — JSON-encodes
  and publishes to Redis channel `user:{user_id}`. Callers (messages router) import exactly this.
  Event shape: `{"type": "<event_type>", ...payload}` e.g. `{"type": "message", "conversation_id": ..., "message_id": ...}`
  — NEVER include ciphertext contents in events beyond the opaque base64 blob field `ciphertext`.
- `app.services.prekey_service`: `async def consume_one_time_prekey(session, device_id) -> OneTimePrekey | None`
  — atomic `UPDATE ... SET consumed_at = now() WHERE id = (SELECT ... WHERE consumed_at IS NULL
  LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING ...`.

### Models

`app/models/` split by domain, mirroring SPEC §3 exactly (types, checks, defaults, indexes):
`identity.py` (User, Campus, Chapter, Membership, ChapterInvite), `e2ee.py` (Device, SignedPrekey,
OneTimePrekey, KyberPrekey), `messaging.py` (Conversation, ConversationMember, Message, MessageReceipt),
`social.py` (Post, PostLike, PostComment), `chirp.py` (Chirp, ChirpVote, ContentReport, UserBlock),
`lineage.py` (Family, LineageEdge), `finance.py` (DuesCycle, LedgerEntry, SpendApproval),
`meetings.py` (Meeting, MeetingAttendance), `alumni.py` (AlumniProfile, JobPost).
`models/__init__.py` re-exports everything (so `Base.metadata` is complete and
`from app import models` works). SQLAlchemy 2.0 style: `Mapped[...]` / `mapped_column`.
UUID PKs: `server_default=text("gen_random_uuid()")`.

### Routers (module → mount; registered in `app.main` exactly like this)

| module | routes |
|---|---|
| `routers/auth.py` | `POST /auth/bootstrap` |
| `routers/chapters.py` | `/chapters` CRUD, `GET/PATCH /chapters/{chapter_id}/members`, `POST /chapters/{chapter_id}/invites`, `POST /chapters/join` (invite code) |
| `routers/keys.py` | `POST /devices`, `POST /devices/{device_id}/prekeys`, `GET /devices/{device_id}/prekeys/count`, `GET /users/{user_id}/prekey-bundle` |
| `routers/messages.py` | `POST /conversations`, `GET /conversations`, `POST /conversations/{conversation_id}/messages`, `GET /conversations/{conversation_id}/messages?before=&limit=`, `POST /conversations/{conversation_id}/leave`, `POST /messages/{message_id}/receipts` |
| `routers/feed.py` | `/chapters/{chapter_id}/posts` CRUD + `/posts/{post_id}/likes`, `/posts/{post_id}/comments` |
| `routers/chirps.py` | `GET/POST /campuses/{campus_id}/chirps`, `PUT /chirps/{chirp_id}/vote`, `DELETE /chirps/{chirp_id}` (author only) |
| `routers/moderation.py` | `POST/GET /moderation/reports`, `POST/DELETE /moderation/blocks`, `POST /moderation/chirps/{chirp_id}/remove` |
| `routers/lineage.py` | `GET /chapters/{chapter_id}/lineage` (nodes+edges+families), `POST /chapters/{chapter_id}/lineage/families`, `POST /chapters/{chapter_id}/lineage/edges`, `POST /chapters/{chapter_id}/lineage/edges/{edge_id}/confirm` |
| `routers/finance.py` | `/chapters/{chapter_id}/dues-cycles`, `GET/POST /chapters/{chapter_id}/ledger`, `/chapters/{chapter_id}/spend-approvals` (+ decide) |
| `routers/meetings.py` | `/chapters/{chapter_id}/meetings` CRUD + `PUT .../meetings/{meeting_id}/attendance` |
| `routers/alumni.py` | `GET/PUT /alumni/profile`, `GET /alumni/directory`, `/jobs` CRUD |
| `routers/payments.py` | `POST /payments/connect/onboarding-link` (stub), `POST /payments/dues/{cycle_id}/intent` (stub), `POST /webhooks/stripe` (signature-check stub ONLY per §9) |

Each router module defines `router = APIRouter(tags=[...])` (prefixes in the paths themselves for
clarity). `app.main.create_app()` includes all twelve + `ws.gateway.router` + `GET /_health`.

### Schemas

`app/schemas/<domain>.py` (same domain split as models). Pydantic v2,
`model_config = ConfigDict(from_attributes=True)`. Naming: `XCreate`, `XUpdate`, `XOut`.
**`ChirpOut` has NO author field of any kind (§8.3).** Ciphertext travels as base64 `str` in
JSON bodies (`ciphertext_b64`), stored as `bytes`.

### Functional vs stub (§9)

FULLY functional: keys, messages (+ prekey_service, pubsub fan-out), chapters, auth bootstrap.
Functional-but-simple (plain DB CRUD is fine): feed, chirps, moderation, lineage, finance, meetings, alumni.
STUB ONLY: payments/stripe_service (raise 501 `NotImplementedError`-style with `# TODO(milestone-8)`),
fcm_service (log-only no-op, content-free signature `send_content_free_push(user_id, title)`).
Do NOT build: Skia tree canvas, encrypted backups.

### Finance rules (§8.2, §2.5)

No UPDATE/DELETE route on ledger entries anywhere. Corrections = `POST .../ledger` with
`entry_type="correction"` + `corrects_entry_id`. The Alembic migration ALSO installs a Postgres
trigger `ledger_append_only` that raises on UPDATE/DELETE of `ledger_entries` — defense in depth.

### Logging (§8.1, §8.6)

Never log ciphertext, tokens, or financial amounts. When logging message flow, log ids only.

---

## Mobile (app-mobile/)

- TypeScript strict. Path alias `@/*` → `src/*`. Expo Router file layout per SPEC §5.
- Design implements `app-mobile/DESIGN.md` (written by the design-recon step from dribbble.com
  references). ALL colors/spacing/type from `src/theme` tokens — no hardcoded hex/px in screens.
  Light + dark palettes; default follows system.
- `src/components/` — shared primitives only (Screen, AppText, Button, Card, Avatar, ListRow,
  Badge, EmptyState, etc.). Screens compose these.
- Screens render from mock data in `src/mocks/data.ts`, accessed through `src/api/*` functions
  typed to the real backend routes (so wiring later = swapping the fetch implementation).
- Crypto/db/realtime/payments/notifications modules are **typed stubs with real interfaces** and
  `// TODO(milestone-N)` markers per SPEC §7. No fake crypto — stubs throw or no-op explicitly.
- `chapter/` tab: role-gated cards (treasurer/secretary tiles only for those roles); `tree.tsx` is
  a placeholder screen (Skia canvas is milestone 6 — do NOT build it).
- Deep link scheme `chirp://` with `join-chapter` route handling invite codes (`app.json` scheme +
  associated domain placeholders).

## Repo root

`docker-compose.yml` (postgres:16 as `db`, user/pass/db all `chirp`, port 5432; redis:7 as
`redis`, port 6379), `README.md` (quickstart), `.gitignore` (python + node + expo + env).

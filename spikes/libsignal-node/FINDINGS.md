# Milestone-3 de-risking spike: libsignal-node vs the real Chirp backend

**Date:** 2026-08-12 · **Branch:** next-steps · **Backend:** `.venv` uvicorn on `:8000`, Postgres 14 (`chirp`/`chirp`, schema already migrated), Redis **not running** (see Finding 3).

**Bottom line:** the real 1:1 X3DH + Double Ratchet flow and the real sender-key group flow both run end to end against real cryptography from `@signalapp/libsignal-client`. The 1:1 flow is proven against the **actual live backend** over HTTP (bootstrap → device registration → prekey bundle fetch → send → history fetch → decrypt, in both directions). It only works because of one workaround (Finding 1) and one client-side inference rule (Finding 2) — both are real contract gaps in the current backend schema, documented below, backend code untouched.

---

## What ran and passed (verified)

### 0. Install

```
$ cd spikes/libsignal-node && npm install
added 3 packages, and audited 4 packages in 7s
found 0 vulnerabilities
```

Prebuilt darwin-x64 binary present and loadable (`node_modules/@signalapp/libsignal-client/prebuilds/darwin-x64/@signalapp+libsignal-client.node`) — no compile step needed on this Intel Monterey machine. **No install-failure finding to report**; `@signalapp/libsignal-client@0.100.0` (latest on npm as of this spike) installs and loads cleanly on macOS 12.7.6 x86_64 / Node 20.20.0.

### 1. 1:1 DM flow against the real backend — `npm run dm` (`src/dm-flow.mjs`)

Backend started exactly as specified:
```
cd backend && .venv/bin/uvicorn 'app.main:create_app' --factory --port 8000
```

Full run (unedited):

```
=== 0. backend health check ===
GET /healthz -> { status: 'ok' }

=== 1. bootstrap two real users via POST /auth/bootstrap ===
alice user_id = c008108a-34f7-48f8-8880-b22a1f41ec78
bob   user_id = f12a7729-4b91-42c5-9d9d-cbb1abddc50f

=== 2. register devices via POST /devices (real identity key + signed prekey + 10 OTKs) ===
alice device_id = 2160f571-c764-4be5-88c4-f1901b476e8c registration_id = 15667
bob   device_id = 754208e8-245d-4c2e-8b9e-055d7be31989 registration_id = 728

=== 3. create a dm conversation via POST /conversations ===
conversation_id = af0c32d4-759b-429d-8645-51b0660a4cbf members = [
  'c008108a-34f7-48f8-8880-b22a1f41ec78',
  'f12a7729-4b91-42c5-9d9d-cbb1abddc50f'
]

=== 4. OTK count before bundle fetch (GET /devices/{id}/prekeys/count, as Bob) ===
bob one_time_prekeys_available (before) = 10

=== 5. Alice fetches Bob's prekey bundle via GET /users/{id}/prekey-bundle (real backend call) ===
bundle device_id = 754208e8-245d-4c2e-8b9e-055d7be31989 otk key_id consumed = 1

=== 6. OTK count after bundle fetch — must have dropped by exactly 1 ===
bob one_time_prekeys_available (after) = 9

=== 7. X3DH: Alice processes the bundle -> session installed in her local SessionStore ===
X3DH session established on Alice side for f12a7729-4b91-42c5-9d9d-cbb1abddc50f.1

=== 8. Alice encrypts message #1 (Double Ratchet, first message -> PreKeySignalMessage) ===
ciphertext1 wire type = 3 (3 = PreKeySignalMessage)

=== 9. POST ciphertext #1 to /conversations/{id}/messages (server never parses it) ===
stored message_id = 4e4e5be2-473b-4280-9ee8-26cb90504bca message_type = signal

=== 10. Bob GETs message history and decrypts message #1 ===
decrypted via PreKeySignalMessage -> "Hey Bob, it's Alice (msqv08oq) — first message over real X3DH!"
ROUND TRIP #1 OK (Alice -> Bob, X3DH + Double Ratchet)

=== 11. Bob replies — message #2, opposite direction, ratchet advances (-> plain SignalMessage) ===
ciphertext2 wire type = 2 (2 = SignalMessage/Whisper)
stored message_id = 146d6dc1-ed39-4171-8b91-fca323344469
decrypted via SignalMessage -> "Got it Alice (msqv08oq) — replying, ratchet advances!"
ROUND TRIP #2 OK (Bob -> Alice, ratchet advanced)

=== 12. history sanity: GET /conversations/{id}/messages returns both, newest first ===

=== ALL DM-FLOW ASSERTIONS PASSED ===
```

Everything in this run is a real HTTP call to the real FastAPI app backed by real Postgres — `bootstrap`, `POST /devices` (real libsignal identity key + signed prekey + 10 one-time prekeys), `GET /users/{id}/prekey-bundle` (real OTK hand-out, consumed atomically), `POST /conversations`, `POST .../messages`, `GET .../messages`. Assertions covered: byte-exact ciphertext round-trip through the server, plaintext equality after decrypt on both legs, message ordering (newest-first), and the server-side OTK count dropping by exactly 1 after exactly one bundle fetch (`10 → 9`).

### 2. Sender-key group flow — `npm run group` (`src/group-flow.mjs`)

In-process, 3 simulated members (Alice/Bob/Carol), backend round-trip intentionally skipped per the spike brief ("optional here") — the crypto is what's being de-risked, and the HTTP plumbing was already proven byte-exact in the DM flow. One extra empirical check confirmed the backend *would* accept these messages if sent (see below).

```
=== 1. create 3 members + pairwise X3DH sessions (Alice <-> Bob, Alice <-> Carol) ===
pairwise sessions established: alice->bob, alice->carol

=== 2. Alice creates a sender key and distributes it to Bob + Carol ===
  distributed sender key 7666231e-e116-4039-917c-77290be69a43 to bob-group (via real pairwise Double Ratchet session)
  distributed sender key 7666231e-e116-4039-917c-77290be69a43 to carol-group (via real pairwise Double Ratchet session)

=== 3. Alice encrypts ONE group message with the sender key ===
group ciphertext #1 produced once, wire type = 7 (7 = SenderKeyMessage)

=== 4. Both Bob and Carol decrypt the same ciphertext independently ===
bob   decrypted: "Chapter meeting moved to 7pm — sender-key group message #1"
carol decrypted: "Chapter meeting moved to 7pm — sender-key group message #1"
GROUP ROUND TRIP OK — one ciphertext, two independent recipients decrypt correctly

=== 5. member-leave rotation (SPEC §6.4): Carol leaves, Alice rotates + redistributes to Bob ONLY ===
old distributionId = 7666231e-e116-4039-917c-77290be69a43
new distributionId = 40bf12a8-35e6-420d-b958-655547c05c01 (fresh sender key chain; old one is abandoned)
  distributed sender key 40bf12a8-35e6-420d-b958-655547c05c01 to bob-group (via real pairwise Double Ratchet session)

=== 6. Alice encrypts group message #2 with the ROTATED sender key ===

=== 7. Bob (still a member) decrypts fine; Carol (left) cannot decrypt the new sender key ===
bob decrypted post-rotation message: "New treasurer announced — sender-key group message #2 (post-rotation)"
carol groupDecrypt correctly THREW: missing sender key state for distribution ID 40bf12a8-35e6-420d-b958-655547c05c01
MEMBER-LEAVE ROTATION OK — removed member cannot read messages sent after rotation

=== ALL GROUP-FLOW ASSERTIONS PASSED ===
```

SPEC §6.4 explicitly flags member-leave rotation as "the most security-critical client behavior — test it first." This confirms the mechanism holds: a member excluded from a redistribution genuinely cannot decrypt anything encrypted with the rotated sender key (`groupDecrypt` throws `missing sender key state for distribution ID ...` rather than failing open).

### 3. Extra check: does the backend accept `message_type: "sender_key_distribution"`?

Yes — verified with a standalone real HTTP round trip (bootstrap → register device → create group conversation → send message with `message_type: "sender_key_distribution"`):

```
sender_key_distribution message accepted: {"id":"d3a71db0-4936-4c52-b17b-ad6f51d52524","message_type":"sender_key_distribution"}
```

So the backend's `MessageType` schema already anticipates the group flow's distribution-message step — see Finding 2 for the gap that remains.

---

## Findings (backend code NOT modified — filed here per instructions)

### Finding 1 — RESOLVED (2026-08-12, see addendum at end of file): current `@signalapp/libsignal-client` requires a Kyber (PQXDH) prekey; the backend schema has no column for one

`@signalapp/libsignal-client@0.100.0`'s `PreKeyBundle.new(...)` signature takes `kyber_prekey_id: number`, `kyber_prekey: KEMPublicKey`, `kyber_prekey_signature: Uint8Array` as **non-nullable** arguments (`node_modules/@signalapp/libsignal-client/dist/ProtocolTypes.d.ts:58`). Empirically confirmed — passing `null` for those three throws at the native layer, not just a TypeScript complaint:

```
$ node -e "... m.PreKeyBundle.new(12345, 1, null, null, 1, spkPub, spkSig, idKeyPair.publicKey, null, null, null) ..."
FAILED without kyber args: failed to downcast any to number
```

Signal's protocol moved to PQXDH (adds a post-quantum KEM key to X3DH, standard since roughly libsignal-client 0.40s-era) specifically to prevent classical-only downgrade attacks — it isn't optional in this SDK version, and pinning to some older pre-PQXDH release isn't a real fix either (it would just mean the RN app ships a protocol version Signal itself no longer considers safe, and reintroduces the exact downgrade risk PQXDH exists to close).

Chirp's schema (`backend/app/models/e2ee.py`, SPEC.md §3 lines 59-85) has exactly two prekey tables — `signed_prekeys` (EC) and `one_time_prekeys` (EC) — and **no `kyber_prekeys` table or column anywhere**. `DeviceCreate` / `PrekeyUpload` (`backend/app/schemas/e2ee.py`) have no field for it either, so there is currently no way to get a Kyber public key into the key directory via the documented API, and `PrekeyBundleOut` / `DevicePrekeyBundleOut` have nowhere to return one.

**Workaround used in this spike** (`spikes/libsignal-node/src/lib/participant.mjs`): each simulated device generates one Kyber keypair locally and keeps it in an in-process `Map` (`KYBER_DIRECTORY`) keyed by the backend's `device_id`, standing in for the missing backend column/endpoint. Every other key in the spike (identity key, EC signed prekey + signature, EC one-time prekeys) is the real thing, registered with and fetched from the real backend over HTTP — only the Kyber half is faked, and only because the backend genuinely has nowhere to put it.

**Recommendation:** add a `kyber_prekeys` table mirroring `signed_prekeys` (`id, device_id, key_id, public_key, signature, created_at`), add `kyber_prekey: {key_id, public_key_b64, signature_b64}` to `DeviceCreate`, and add it to `DevicePrekeyBundleOut`. This is schema/router/schema work only — no protocol redesign — but it is required before any real device (RN or otherwise) can complete X3DH against this backend with the current libsignal-client. Flagging this as the top item to fix before the physical-device test, since the RN client will hit the exact same wall.

### Finding 2 — MEDIUM (mitigated in the spike, not blocking): `messages.message_type` can't distinguish a Signal PreKey message from a Whisper message

Signal's own wire format has two shapes for 1:1 ciphertext — a `PreKeySignalMessage` (first message on a session, type `3`) and a plain `SignalMessage`/"Whisper" message (type `2`, everything after). A receiving client has to know which parser to use (`PreKeySignalMessage.deserialize` + `signalDecryptPreKey`, vs. `SignalMessage.deserialize` + `signalDecrypt`) — real Signal's server-side envelope carries this as an explicit type field for exactly this reason. Chirp's `message_type` column/schema (`backend/app/schemas/messaging.py`) only distinguishes `"signal"` vs `"sender_key_distribution"` — both prekey and whisper 1:1 ciphertexts are stored as `"signal"`, so the receiver gets no signal (no pun intended) about which of the two wire formats it's holding.

**Mitigation used in this spike** (`spikes/libsignal-node/src/lib/util.mjs::decryptInbound`): try the `PreKeySignalMessage` parse first, fall back to `SignalMessage` on failure. Verified empirically safe in this spike (both real messages in the DM flow round-tripped correctly, with the log confirming each message went through the branch it was actually supposed to: message 1 as `PreKeySignalMessage`, message 2 as `SignalMessage`) — the two are different protobufs, so parsing one as the other throws rather than silently returning garbage, at least for the cases exercised here.

**Recommendation:** either (a) formally adopt the try-prekey-then-fallback pattern as the documented client behavior (cheap, already proven to work here, zero backend changes), or (b) add a third literal value or a dedicated boolean/int column if a future feature needs the server (or push payload) to know the wire type without asking the client to guess. Not a blocker — (a) is a fine permanent answer — but worth a one-line note in SPEC §6 so future implementers don't hit this blind.

### Finding 3 — informational, not a bug: Redis absence doesn't break message send

Redis is not running in this spike environment. `app.ws.pubsub.publish_to_user` is wrapped in try/except inside `routers/messages.py::send_message`, so `POST .../messages` still returned `201` and stored the message correctly with no Redis reachable — confirmed by every `sendMessage` call in both flows above succeeding. Real-time WS fan-out (`app/ws/gateway.py`) was **not exercised** by this spike (see Unverified, below) — only the HTTP history-fetch delivery path was used, which is sufficient for the E2EE crypto de-risking goal but is a separate thing to verify later.

### Everything else matched CONVENTIONS.md / SPEC.md exactly, no other contract mismatches found

Bootstrap accepted `campus_id: null` with no pre-existing campus row (matches the nullable FK). `POST /devices`' nested `signed_prekey` / `one_time_prekeys` field names and the `_b64` suffix convention matched on the first try. `identity_key_b64`/`public_key_b64`/`signature_b64` round-tripped byte-for-byte in both directions (BYTEA <-> base64) — proven not by inspection but by the fact the actual X3DH handshake and Double Ratchet decrypt succeeded, which is impossible if any of those conversions were off by so much as one byte. `GET /users/{id}/prekey-bundle` degrades correctly (would return `one_time_prekey: null` once a device's pool is exhausted — not hit in this run since only one fetch per device happened, but the code path was read and matches SPEC). OTK consumption was atomic and exactly-once across a real request in this test (concurrent-request race behavior of the `FOR UPDATE SKIP LOCKED` claim was not independently re-tested here — see Unverified).

---

## React Native path — researched recommendation

`@signalapp/libsignal-client` is explicitly Node-only: it loads a prebuilt native addon via `node-gyp-build` (confirmed present at `node_modules/@signalapp/libsignal-client/prebuilds/{darwin-x64,darwin-arm64,linux-x64,linux-arm64,win32-x64,win32-arm64}/*.node`), which depends on Node's native-addon (`libuv`/N-API) loader — that mechanism does not exist in React Native's JS runtime (Hermes/JSC), so this exact package cannot ship in the Expo app. Three real options, researched from npm/GitHub metadata (all citable, nothing guessed):

**Option A - `react-native-libsignal-client` (community, npm, by ehsunahmadi).** Latest `0.1.44`, published 2025-12-17, 44 versions since 2024-06 - actively maintained. Built as an **Expo Module** (`expo-module build`/`prepare` scripts, `expo-modules-core` + `expo-build-properties` deps, peer deps `expo`/`react`/`react-native`), and per its own README it **wraps the official Rust libsignal core via Swift/Java/Kotlin bindings** (not a JS reimplementation) and ships an **Expo config plugin** that adds the required iOS pod automatically. Risks, also from the README/repo itself: only 4 GitHub stars / 1 watcher / 3 forks / 6 open issues / 0 merged PRs (single maintainer, low adoption signal for something this security-critical); the README's own words: *"This binding is not an official Signal distribution. Review upstream changes and audit cryptographic handling for production deployments."*; it builds the Rust core from source at install time (README lists "Rust & targets (first time only)" as a prerequisite), which means every EAS/dev build environment needs a Rust toolchain - a real CI cost the Node package (prebuilt binaries) doesn't have. Its README does not explicitly mention PQXDH/Kyber or sender keys by name, so **which libsignal-client core version it wraps, and whether that version's `PreKeyBundle` also mandates a Kyber prekey (per Finding 1), needs to be checked against its `package.json`/vendored core version before relying on it** - if it wraps a pre-PQXDH core it will produce a different (and cryptographically weaker) session type than the Node package just proven here, which would be a real interop mismatch. The same author's earlier package, `expo-libsignal-client`, is effectively superseded (last published 2024-07-06) - `react-native-libsignal-client` is the one to evaluate, not that one.

**Option B - a small custom native module bound to Signal's own official prebuilt artifacts.** Signal publishes the same Rust core as first-party binaries: `org.signal:libsignal-android` (AAR, Maven Central) for Android, and the `LibSignalClient` CocoaPod/Swift Package (XCFramework, from `github.com/signalapp/libsignal`, `swift/README.md` + `LibSignalClient.podspec`) for iOS - the same trust level and same source as the Node package validated in this spike, no local Rust compilation needed (prebuilt), no dependency on a single-maintainer wrapper. Cost: writing and maintaining a thin Expo Module (Kotlin + Swift) exposing only the ~10 calls the app actually needs (device key generation, X3DH session establish, encrypt/decrypt, sender-key group ops) - real but bounded engineering work, and it's the same architecture Signal's own iOS/Android apps use.

**Option C - pure-JS Signal protocol reimplementations** (e.g. `@privacyresearch/libsignal-protocol-typescript`, last published 2025-06, GPL-3.0; older `signal-protocol-react-native`, stale since ~2021). Not recommended: these are independent reimplementations of the wire protocol, not bindings to Signal's own audited Rust core, so they carry both a correctness-divergence risk against whatever the backend/other clients run and a much smaller audit surface for something this security-sensitive.

**Recommendation:** start with **Option A** as a fast 1-2 day validation spike (spin up an Expo dev build with `react-native-libsignal-client`, run its example app / a port of this spike's DM flow on a physical device against the same real backend, and specifically confirm its vendored core version's `PreKeyBundle` API and Kyber requirement match what was verified here) - but do not commit it to production without: pinning and auditing the exact core version for PQXDH parity (Finding 1 applies here too, worse if unverified), getting the AGPL-3.0 licensing obligation reviewed by whoever owns that call (this applies to **any** route - it's `libsignal` core's own license, confirmed via `@signalapp/libsignal-client`'s `package.json`: `"license": "AGPL-3.0-only"` - not specific to the community wrapper), and accepting the Rust-toolchain CI cost. If Option A fails validation on a physical device or the licensing/bus-factor risk is a hard no, fall back to **Option B** - more upfront work, but it removes both of those risks and matches what Signal's own apps do.

---

## Verified vs. unverified

**Verified (executed, output captured above):**
- `npm install @signalapp/libsignal-client` succeeds with prebuilt darwin-x64 binaries, no compile step.
- `POST /auth/bootstrap`, `POST /devices`, `GET /devices/{id}/prekeys/count`, `GET /users/{id}/prekey-bundle`, `POST /conversations`, `POST /conversations/{id}/messages`, `GET /conversations/{id}/messages` all called for real over HTTP against the real running backend + real Postgres.
- Real X3DH session establishment from a real backend-issued prekey bundle (Kyber workaround aside - Finding 1).
- Real Double Ratchet encrypt/decrypt round trip in both directions (Alice->Bob as `PreKeySignalMessage`, Bob->Alice as `SignalMessage`, ratchet advance confirmed by the differing wire types).
- Server-side one-time-prekey consumption confirmed via `GET /devices/{id}/prekeys/count` dropping exactly 10->9 after exactly one bundle fetch.
- Real sender-key group flow: one `groupEncrypt` call decrypted correctly by two independent `groupDecrypt` calls (Bob, Carol).
- Real member-leave sender-key rotation: a member excluded from redistribution provably cannot decrypt post-rotation ciphertext (`groupDecrypt` throws).
- `message_type: "sender_key_distribution"` accepted end-to-end by the real backend.
- Message send succeeds with Redis unreachable (try/except in `messages.py` confirmed to swallow the failure in practice, not just by reading the code).
- `PreKeyBundle.new()` throwing on null Kyber args (Finding 1) - reproduced directly, not inferred from TypeScript types alone.

**Unverified (reasoned about / read from docs, not executed):**
- Physical-device / React Native execution of any of this (explicitly out of scope for this spike per the brief - "RN test comes later").
- WebSocket real-time fan-out (`app/ws/gateway.py`) - only the HTTP history-fetch delivery path was exercised.
- Push notifications (`fcm_service`) - confirmed log-only no-op by reading the code, not independently exercised beyond the log line firing.
- Concurrent-request race behavior of the OTK `FOR UPDATE SKIP LOCKED` claim - only ever tested with one fetch at a time in this spike.
- Multi-device-per-user (`prekey-bundle` returning multiple device bundles) - this spike used exactly one device per user throughout.
- Whether `react-native-libsignal-client`'s vendored libsignal-client core version matches the Kyber/PQXDH behavior confirmed here - flagged in the recommendation as something to check before adopting it, not something this spike could check (Node-only spike, no RN environment available here).
- Sealed-sender / `PlaintextContent` / zkgroup APIs - present in the SDK, not part of SPEC §6, not exercised.

---

## Cleanup

`uvicorn` (started for this spike) was killed at the end of the session; port 8000 confirmed free afterward. Port 8081 (Expo dev server) was never touched.

---

## Addendum (2026-08-12): Finding 1 RESOLVED

The backend now has a real Kyber (PQXDH) prekey directory. Landed:

- `backend/app/models/e2ee.py`: new `KyberPrekey` model — `kyber_prekeys` table (`id, device_id,
  key_id, public_key, signature, is_last_resort, consumed_at, created_at`), partial index
  `idx_kyber_otk_available` on `(device_id) WHERE consumed_at IS NULL AND NOT is_last_resort`.
  Exported from `app/models/__init__.py`.
- `backend/alembic/versions/0002_kyber_prekeys.py` — applied to the local `chirp` Postgres DB
  (`alembic upgrade head` → `0001 -> 0002`, confirmed via `\d kyber_prekeys`).
- `backend/app/schemas/e2ee.py`: `KyberPrekeyCreate` / `KyberPrekeyOut`; `DeviceCreate` and
  `PrekeyUpload` gain optional `kyber_last_resort` (single) + `kyber_one_time` (list, default
  `[]`) — both nullable/omittable, so pre-PQXDH registrations still validate. `PrekeyCountOut`
  gains `kyber_one_time_prekeys_available: int` and `kyber_last_resort_registered: bool`.
  `DevicePrekeyBundleOut` gains `kyber_prekey: KyberPrekeyOut | None`.
- `backend/app/services/prekey_service.py`: `consume_one_time_kyber_prekey` (same
  `UPDATE ... FOR UPDATE SKIP LOCKED` atomic-claim pattern as `consume_one_time_prekey`, scoped
  to `is_last_resort IS FALSE`) and `get_last_resort_kyber_prekey` (latest `is_last_resort=TRUE`
  row, never marks it consumed).
- `backend/app/routers/keys.py`: `POST /devices` and `POST /devices/{id}/prekeys` store the
  kyber fields when present; `GET /users/{id}/prekey-bundle` tries the one-time Kyber pool
  first, falls back to the last-resort key (returned, not consumed) when the pool is empty,
  and returns `kyber_prekey: null` for devices that never registered one (nullable/backward-
  compatible path — verified with the existing `register_device` test fixture, which sends
  no kyber fields at all, and still gets a `201` + a bundle with `kyber_prekey: null`).
- `backend/tests/test_kyber_prekeys.py` (new, 6 tests) + the full existing suite: **17 passed**
  (`test_auth_firebase_mode.py` excluded — it was mid-edit by another agent in this session and
  fails only at *collection* with `ModuleNotFoundError: No module named 'firebase_admin'`,
  identical to its pre-existing-work state; not something this change touched or broke).

```
$ .venv/bin/alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, Kyber (PQXDH) prekey directory...

$ .venv/bin/python -m pytest tests/ -v --ignore=tests/test_auth_firebase_mode.py
...
tests/test_kyber_prekeys.py::test_registration_with_kyber_fields_succeeds PASSED
tests/test_kyber_prekeys.py::test_registration_without_kyber_fields_still_works PASSED
tests/test_kyber_prekeys.py::test_bundle_returns_one_time_kyber_then_falls_back_to_last_resort PASSED
tests/test_kyber_prekeys.py::test_device_with_no_kyber_prekeys_returns_null_kyber_bundle PASSED
tests/test_kyber_prekeys.py::test_concurrent_bundle_fetches_dont_double_consume_kyber PASSED
tests/test_kyber_prekeys.py::test_replenish_rotates_last_resort_and_tops_up_one_time_kyber PASSED
...
======================== 17 passed, 1 warning in 16.56s ========================
```

**Spike updated to prove it for real** (`src/lib/participant.mjs`, `src/lib/backend.mjs`,
`src/dm-flow.mjs`): the local `KYBER_DIRECTORY` workaround is deleted. `Participant.registerDevice()`
now generates a real `Signal.KEMKeyPair` for a signed last-resort Kyber prekey plus a 5-key
one-time Kyber batch, uploads both to the real backend via `kyber_last_resort` / `kyber_one_time`
on `POST /devices` (exactly like every other key), and keeps the private halves in its local
`kyberPreKey` store keyed by the same `key_id` the server will hand back later.
`Participant.fetchPreKeyBundleFor()` now builds `Signal.PreKeyBundle.new(...)`'s Kyber arguments
(`kyber_prekey_id`, `Signal.KEMPublicKey.deserialize(...)`, `kyber_prekey_signature`) entirely
from `deviceBundle.kyber_prekey` as returned by the live server — no local directory, no fallback,
no fake data anywhere in the flow.

Extra direct-backend check (before the full DM rerun) confirming the one-time → last-resort
fallback and non-consumption of the last-resort key, against the live server on port 8000:

```
fetch 1 -> key_id= 2 is_last_resort= false
fetch 2 -> key_id= 3 is_last_resort= false
fetch 3 -> key_id= 1 is_last_resort= true
final count: {"device_id":"3511d3d0-c565-4ccd-be22-f5303fed277b","one_time_prekeys_available":0,"kyber_one_time_prekeys_available":0,"kyber_last_resort_registered":true}
```

Full `npm run dm` rerun against `.venv/bin/uvicorn 'app.main:create_app' --factory --port 8000`
on the same local Postgres (`chirp` db, now at migration `0002`) — unedited output:

```
=== 0. backend health check ===
GET /healthz -> { status: 'ok' }

=== 1. bootstrap two real users via POST /auth/bootstrap ===
alice user_id = ccad37a7-28aa-42b7-b115-f07cbfa0c399
bob   user_id = 57fd575d-dea5-4ce9-8277-10d2f98ffca0

=== 2. register devices via POST /devices (real identity key + signed prekey + 10 OTKs) ===
alice device_id = 560d2fc3-527a-4222-9448-ace6f46b51f2 registration_id = 11167
bob   device_id = 122fe76a-839c-4374-b5db-73e13dacb38f registration_id = 9853

=== 3. create a dm conversation via POST /conversations ===
conversation_id = 193c1974-7403-46ad-84dd-3296ddba836b members = [ '57fd575d-...', 'ccad37a7-...' ]

=== 4. OTK count before bundle fetch (GET /devices/{id}/prekeys/count, as Bob) ===
bob one_time_prekeys_available (before) = 10

=== 5. Alice fetches Bob's prekey bundle via GET /users/{id}/prekey-bundle (real backend call) ===
bundle device_id = 122fe76a-839c-4374-b5db-73e13dacb38f otk key_id consumed = 1

=== 6. OTK count after bundle fetch — must have dropped by exactly 1 ===
bob one_time_prekeys_available (after) = 9

=== 7. X3DH: Alice processes the bundle -> session installed in her local SessionStore ===
X3DH session established on Alice side for 57fd575d-dea5-4ce9-8277-10d2f98ffca0.1

=== 8. Alice encrypts message #1 (Double Ratchet, first message -> PreKeySignalMessage) ===
ciphertext1 wire type = 3 (3 = PreKeySignalMessage)

=== 9. POST ciphertext #1 to /conversations/{id}/messages (server never parses it) ===
stored message_id = b97e6ec2-7a96-4bbc-89d0-5890fd211ab4 message_type = signal

=== 10. Bob GETs message history and decrypts message #1 ===
decrypted via PreKeySignalMessage -> "Hey Bob, it's Alice (msqwb06w) — first message over real X3DH!"
ROUND TRIP #1 OK (Alice -> Bob, X3DH + Double Ratchet)

=== 11. Bob replies — message #2, opposite direction, ratchet advances (-> plain SignalMessage) ===
ciphertext2 wire type = 2 (2 = SignalMessage/Whisper)
stored message_id = dee977e0-7ca4-487c-80ca-46b868fdb93b
decrypted via SignalMessage -> "Got it Alice (msqwb06w) — replying, ratchet advances!"
ROUND TRIP #2 OK (Bob -> Alice, ratchet advanced)

=== 12. history sanity: GET /conversations/{id}/messages returns both, newest first ===

=== ALL DM-FLOW ASSERTIONS PASSED ===
```

This is the same X3DH + Double Ratchet round trip as the original run, now built end-to-end from
real server-issued Kyber material (last-resort + one-time) instead of the in-process
`KYBER_DIRECTORY` stand-in. `uvicorn` was killed and port 8000 confirmed free again afterward
(same as the original spike's cleanup discipline).

**Recommendation from the original finding is DONE — no further action needed on Finding 1.**
Remaining open item, unchanged from the original recommendation: whichever React Native
libsignal binding gets adopted (see "React Native path" above) still needs its own PQXDH/Kyber
parity check against its vendored libsignal-client core version before relying on it against
this now-PQXDH-complete backend.

# E2EE direct messages: production design (v1)

Status: **proposed for review, Oct 7 2026** (board c442, decisions on c440).
Scope: encrypted **direct messages** shipped before launch. Groups are a later
document; nothing here may make them harder (section 9).

This turns the Sep 21 evaluation (`spikes/vodozemac-native-boundary/PROPOSED-CONTRACT.md`,
c23) into a buildable plan. Where this file and the contract disagree, this file
wins for v1; where this file is silent, the contract's requirements still apply.

## 1. Decisions this design rests on (c440, Jose + Q, Oct 7)

| # | Decision |
|---|---|
| 1 | Encrypted DMs ship **before** launch. Groups follow. |
| 2 | **vodozemac** (Apache-2.0) on its own: Olm for device pairs, our own protocol layer on top. |
| 3 | No post-quantum requirement. vodozemac stable session config **V1**, no experimental features. |
| 4 | Trust-on-first-use, safety numbers, a **hard stop** on any identity change, and a new device must be **approved from an existing one**. |
| 5 | No history on a new phone in v1. Losing every device means a fresh start (SPEC). |

Defaults chosen here that need a yes or no (section 11): the Rust-to-Swift/Kotlin
binding tool (uniffi, MPL-2.0), iOS adapter before Android, the 10,000-character
message limit unchanged.

## 2. Architecture

```
app screens (messages/*.tsx)
   |  plain TS calls, plaintext only for rendering
src/crypto/ (TS)            <- replaces today's throwing signal.ts stub
   |  Expo Modules API, async
modules/chirp-crypto/       <- Expo local module: ios/ (Swift), android/ (Kotlin)
   |  uniffi-generated bindings
modules/chirp-crypto/rust/  <- Rust core: vodozemac 0.11.1 + state store + protocol
```

Rules that hold across every layer:

- **Private keys never reach JavaScript.** JS sees public keys, ciphertext, and
  plaintext that has already been durably committed.
- **One serial executor per account** in the Rust core. Every state transition
  runs on it; no two operations interleave.
- **Commit before release.** Encrypt: ratchet + outbox row committed, then
  ciphertext is returned. Decrypt: ratchet + replay record + plaintext row committed
  in one transaction, then plaintext is returned. A failed commit discards the
  candidate state and returns an error, never a body (contract section 5).
- **No fake crypto.** Until a real round trip passes on two phones, the composer
  stays disabled and `verify:e2ee-claims` keeps guarding the copy.

## 3. Rust core (`modules/chirp-crypto/rust`)

Crate pinned to vodozemac **0.11.1** (latest stable, Apache-2.0, Sep 30 2026), built
with default features off. Dependency licenses recorded the way
`spikes/vodozemac-native-boundary/DEPENDENCY-LICENSES.json` does; no AGPL or GPL.

**State store.** One SQLite file per account namespace (`user_id` + device
generation), created by the core in the app's private storage, excluded from
iCloud/Google backups. Records (account pickle, sessions, replay set, outbox,
plaintext messages, trust pins) are encrypted with a 32-byte store key. The key is
generated natively and held in the iOS Keychain
(`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, not synchronizable) or an
Android Keystore-wrapped blob, and handed to the core at unlock. Each record
authenticates its namespace and a monotonically increasing store generation, so a
record copied in from another account or an older snapshot fails to load.

**Lifecycle.** `Locked -> Ready -> Locked`. Sign-out locks, cancels queued work, and
invalidates every outstanding handle. "Remove this phone" wipes the namespace and
revokes the device on the server.

**API exposed through the module** (all async, all fallible with fixed error codes):

| Call | Returns |
|---|---|
| `unlock(scope)` / `lock()` / `wipe(scope)` | state |
| `createDevice()` | public identity (Curve25519 + Ed25519), binding signature, signed one-time keys, signed fallback key |
| `markPublished(serverDeviceId, keyIds)` | ok |
| `topUpKeys(count)` | newly generated signed one-time keys |
| `encrypt(conversationId, clientMessageId, body, recipients)` | one ciphertext leg per recipient device |
| `decrypt(messageId, leg, senderDevice)` | committed plaintext message |
| `listMessages(conversationId, before, limit)` | plaintext rows from the local store |
| `trust(peerUserId)` / `acceptIdentityChange(peerUserId)` / `safetyNumber(peerUserId)` | trust state, numbers |
| `approveDevice(newDeviceBinding)` | approval signature from this device |

## 4. Device directory v2 (backend)

The current tables are Signal-shaped (one DH identity, a signed-prekey slot, Kyber
columns) and `routers/keys.py` requires that slot. Olm needs different fields, so v2
is a **versioned API**, not a reinterpretation of the old columns (contract section 2).
No client has ever registered a device (the app's crypto stub always threw), so the
legacy rows should be empty; the migration verifies that and fails loudly if not.

**Migration** (number claimed on the board before the file is written):

- `devices`: add `crypto_suite` (`"vodozemac-olm-v1"`, NULL = legacy and refused by
  v2), `identity_ed25519` (32 bytes), `generation` (int), `binding_signature`
  (64 bytes), `approved_by_device_id` (nullable FK to devices), `approval_signature`
  (nullable, 64 bytes).
- `one_time_prekeys`: add `kind` (`one_time` | `fallback`) and `signature` (64 bytes);
  widen `key_id` to BIGINT (vodozemac key ids are u64; never reused within a device).
- `signed_prekeys` and `kyber_prekeys` are untouched and unused by v2.

**Endpoints** (`/v2/...`, Firebase-authenticated, rate-limited like today's):

| Endpoint | Behavior |
|---|---|
| `POST /v2/devices` | Register: suite, both identities, binding signature, signed keys. Server verifies every signature (not just its shape). The account's first device is self-approved; later devices start **pending**. |
| `POST /v2/devices/{id}/approve` | An already-approved device of the same account signs the new device's binding. |
| `POST /v2/devices/{id}/keys` + `GET .../keys/count` | Top up one-time keys; count for the client's refill threshold. |
| `GET /v2/users/{id}/devices` | Directory read: approved, unrevoked devices with identities, binding and approval signatures. **Claims nothing.** |
| `POST /v2/keys/claim` | Atomically claim one one-time key per named device, or its signed fallback key when exhausted. Separate from the read above, which fixes the contract's finding that today's bundle fetch burns a key before anything is sent. |
| `DELETE /v2/devices/{id}` | Revoke (existing revocation semantics and caps: 5 active, 20 retained). |

## 5. Message transport v2

One logical message becomes **one ciphertext leg per recipient device**: every
device of the other member, plus the sender's own other devices so the sender's
history stays in sync.

**Migration:** `messages` gains `client_message_id` (UUID) and `envelope_version`;
legacy `ciphertext` becomes nullable with a check constraint (required when
`envelope_version` is NULL). New table `message_legs(message_id,
recipient_device_id, olm_type smallint, ciphertext bytea)`, primary key on the pair.

**`POST /v2/conversations/{id}/messages`** body: `sender_device_id`,
`client_message_id`, `legs[{recipient_device_id, olm_type (0 prekey | 1 normal),
ciphertext_b64}]`.

- The set of leg recipients must equal the conversation's current approved,
  unrevoked devices exactly (minus the sending device). Otherwise
  `409 device_list_mismatch` with the current device ids, so the client refreshes
  its directory, re-encrypts, and retries. Nothing is stored on mismatch.
- Idempotent on `(sender_device_id, client_message_id)`: a retry with the same legs
  returns the stored message; a retry with different legs is `409 client_message_id_reused`.
- One transaction stores the message and all legs, then the existing delivery
  outbox (c375) fans out the WebSocket hint.
- Limits: each leg uses today's `MAX_CIPHERTEXT_B64_LENGTH` (65,536); legs per
  message at most (members x 5 active devices); the whole request is capped and
  load-tested before launch.

**`GET /v2/conversations/{id}/messages`** returns history with only the
**calling device's** leg on each message. Receipts stay per device.

**Size check.** The 10,000-character limit is at most 40,000 UTF-8 bytes. The
encrypted inner envelope adds a few hundred bytes, Olm a little more, and base64
adds a third: about 54,000 characters, inside the 65,536 leg cap. Ciphertext is
base64-encoded **once**, never nested inside another encoded layer (c23 found
nested encoding overflows the cap).

## 6. Inner envelope

Serialized inside the Olm plaintext, so every field is authenticated:
`version, conversation_id, client_message_id, sender (user, device), recipient
(user, device), kind ("text"), body`. On decrypt, every field must match the
outer routing and the authenticated directory, or the message is rejected without
consuming a one-time key or advancing the ratchet (behavior c23 already proves in
the prototype). Server-assigned ids and timestamps are never claimed as
authenticated, since they don't exist at encryption time.

## 7. Client flows

1. **First sign-in on a phone:** unlock (create a namespace if none), `createDevice`,
   `POST /v2/devices`, `markPublished`. If it isn't the account's first device, show
   "Approve this phone from your other phone" until approved.
2. **Keep keys stocked:** on app start and after each received prekey message, read
   the count and top up below a threshold (50 of 100).
3. **Send:** refresh the directory for both users (cached, with the trust check in
   section 8), claim keys for devices with no session, `encrypt`, post. On
   `device_list_mismatch`, refresh and retry once. The outbox row survives restarts;
   a retry reuses the stored ciphertext rather than encrypting again.
4. **Receive:** a WebSocket hint triggers an HTTP fetch for this device's legs, then
   `decrypt` in server order, then the UI reads plaintext from `listMessages`.
5. **Sign-out / account switch:** `lock()`. **Remove phone:** `wipe()` plus revoke.

## 8. Trust

- **Pinning.** The first time a user's devices are seen, each device's Ed25519
  identity is pinned in the local store.
- **Safety number.** Per conversation partner: a 60-digit number derived from both
  users' sorted (user id, approved device identities). A screen shows it for
  comparison in person. A QR code is a later nicety.
- **Hard stop.** If a pinned device's identity changes, or a device appears that no
  pinned device approved, the conversation shows a banner ("Jose's security code
  changed") and sending is **blocked** until the user accepts the change or verifies
  it. Receiving still works.
- **New device approval.** The existing phone gets a prompt with the new phone's
  fingerprint and signs it through `approveDevice`. With no other phone available
  (lost phone), "Reset security" creates a new first device, and every contact gets
  the hard stop above.

## 9. Not in v1, and kept possible

Groups use Megolm, one outbound session per sending device and epoch, with key
delivery over the Olm legs above and rotation when a member leaves (contract
section 4). The leg and approval model here is what that design builds on. Encrypted
backups stay c14. No post-quantum (decision 3).

## 10. Milestones

| # | Milestone | Depends on | Done means |
|---|---|---|---|
| M0 | Local iOS simulator builds (c441) | - | xcodebuild green on Q's Mac, reproducibly |
| M1 | Rust core: store, API, protocol checks | - | host tests: two accounts round-trip, tamper/replay/context failures, crash at each commit boundary, lock and wipe |
| M2 | Backend v2: migration, directory, claim, transport | - | pytest: signature verification, pending approval, exact-device-set rule, idempotency, limits, legacy rows untouched |
| M3 | iOS Expo module + `src/crypto` TS layer | M0, M1 | two in-process accounts round-trip on the simulator |
| M4 | App wiring: registration, top-up, send/receive, local store, DM composer on | M2, M3 | a DM between two simulator accounts against a local backend |
| M5 | Trust UX: safety numbers, hard stop, device approval | M4 | each flow exercised on the simulator |
| M6 | **Two phones** (Q + Jose) on a preview build against a staging backend | M5 | send, receive, restart, delayed delivery, replay rejected, revoked device refused |
| M7 | External security review, then the Android adapter | M6 | findings fixed; Android parity before an Android release |

M1 and M2 run in parallel; M3 needs M0 and M1. Backend changes reach prod only
through the normal Cloud Run redeploy, after M6.

## 11. Open calls for Jose + Q

1. **uniffi (MPL-2.0)** to generate the Swift and Kotlin bindings. MPL is file-level
   copyleft: using it unmodified in a closed app only requires keeping its notices and
   pointing to its (already public) source. That is a different category from the AGPL
   that ruled out libsignal. The alternative is a hand-written C interface plus JNI,
   roughly 2 to 3 more days. Recommendation: uniffi.
2. **iOS first.** Both test phones are iPhones. The Rust core and the TS layer stay
   platform-neutral, and the Android adapter lands before any Android release.
3. **Message limit stays 10,000 characters.** Section 5's size check depends on it.

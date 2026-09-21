# Proposed Chirp native crypto contract

Status: **evaluation proposal, September 21, 2026; not a production selection**.
This is a concrete review target for c23. It does not amend SPEC, select a security
profile, approve a backend migration, or enable the composer. The implementation
in this directory demonstrates only the subset called out below.

## 1. Profile and version negotiation

The candidate is vodozemac 0.11.0 Olm for device pairs and Megolm for group senders.
The probe uses stable session configuration V1, without default or experimental
features. This is **non-PQ**, and V1 uses truncated MACs. Adopting that profile needs
an explicit security decision; switching to experimental V2 is a separate choice.
Neither Chirp's `Conversation.protocol_version=2` nor nullable Kyber columns selects
a vodozemac profile. Never silently downgrade or label Olm as Signal/PQXDH.

Proposed clients negotiate an explicit suite identifier and application-envelope
version. Unknown/disabled suites, versions, message kinds and wire subtypes fail
before plaintext is released. Legacy and proposed records must be distinguishable
in the API and storage; incompatible clients cannot send into the new protocol.

## 2. Versioned device directory and trust

Proposed record fields:

| Field | Required meaning |
| --- | --- |
| `directory_version`, `suite` | Explicit directory format and approved algorithm/profile, independent of database protocol version. |
| `account_id`, `device_id`, `generation` | Canonical UUIDs plus a monotonically increasing registration generation; device identity replacement is visible. A new identity is a new device, not an overwrite. |
| `identity_curve25519`, `identity_ed25519` | Separate 32-byte DH and signing identities. Use canonical padded base64 at the JSON boundary; neither is the other's signing key. |
| `binding_signature` | Ed25519 signature over a domain-separated, unambiguous encoding of format, suite, account, device, generation and both public identities. |
| `prekeys` | Each record has an opaque non-reused public ID, key kind (one-time or fallback), 32-byte Curve25519 key, directory generation and Ed25519 signature binding all those fields and device identity. |
| `revoked_at` | Server authorization state, authenticated on directory fetch and checked again on send. A self-signature alone cannot prove current authorization. |

Recommended review decision: use first-contact fingerprint pinning with a visible
verification path; stop encryption on an unexpected identity change. Adding another
device needs either approval from an already trusted device or explicit user
verification. Firebase-authenticated registration is necessary authorization, but
does not prevent a compromised directory from substituting a fresh self-signed
identity. Account recovery and loss of every trusted device need a visible trust
reset. These trust/UI flows are **proposed, not implemented**.

The exact canonical production codec needs shared test vectors before approval.
This probe signs a fixed JSON tuple of domain, short account/device fixture labels,
DH key and signing key. Altering those fields fails verification. It omits the
production suite/generation/registration fields and **does not authenticate the OTK**
passed to `outbound`; that argument is trusted test input. Do not reuse the probe's
directory struct as the production API.

The directory must persist the mapping between opaque API prekey IDs and native
`KeyId` values. Native IDs are `u64`; the current API uses bounded signed-32-bit
integers. Mapping must survive restart, never wrap/reuse an ID within a device
generation, and remain available until delayed messages expire. Fetch currently
claims an OTK before a sender delivers anything. Publication must not delete its
private key; consume it only after valid inbound session creation plus durable
commit. Exhausted OTK behavior must be explicit: proposed signed fallback keys
allow continued initiation, with expiry and retention of old private fallback
keys for delayed messages. Rate/retention limits remain enforced.

Current incompatibilities: `backend/app/schemas/e2ee.py` requires one DH identity
and a Signal-shaped signed prekey, does not expose the Ed25519 identity, and checks
signature shape only. `routers/keys.py` currently requires that signed-prekey slot
on bundle fetch. A versioned API change is required for this proposal; whether
existing SQL columns can be reused is still a migration design question. Silently
packing both keys or substituting a fallback key into the old slot is not approved.

## 3. Per-device authenticated transport

Proposed authenticated inner context, serialized with the plaintext before Olm
encryption: suite, envelope version, conversation UUID, client-generated logical
message UUID, sender account/device, recipient account/device, content kind and
group epoch. Every field must match authenticated directory/session identity and
canonical routing before returning a body. Server-assigned message IDs/timestamps
do not exist at encryption time; never claim they were authenticated pre-send.

The native Olm prekey/normal subtype (0/1) travels with ciphertext and is decoded
strictly. Outer routing is an untrusted selector, then checked against the decrypted
context. The probe exercises this comparison, including initial-message rejection
without consuming the OTK and normal-message rejection without advancing the live
ratchet. Olm has no application AAD argument in this interface; the inner context
is an application framing proposal, not a new cryptographic primitive.

Proposed API shape: a logical message has one separately bounded ciphertext leg per
recipient device, including the sender's other active devices. Each leg has explicit
sender/recipient device, suite/version and subtype. Canonical HTTP history returns
only authorized legs for the caller's device, grouped by logical message ID. The
server commits a complete validated batch atomically and uses the logical ID plus
sender/device as an idempotency key; conflicting reuse is rejected. Reusing an
existing ciphertext on retry must not run encryption again. The exact total batch
byte/device ceiling is a required implementation choice, with load tests; do not
raise the current per-ciphertext ceiling to accommodate an unbounded JSON array.

Current `MessageCreate` has one opaque ciphertext and a Signal-shaped enum;
`routers/messages.py` stores/broadcasts one row to member users. Extra device/suite
fields are not a targeting mechanism. WebSocket hints omit message type, so receivers
need canonical HTTP routing or a fully self-describing validated envelope. This
proposal therefore needs versioned API/storage/fan-out/idempotency behavior. It
cannot be delivered by changing only the crypto stub.

The prior [schema evaluation](../vodozemac-evaluation/README.md) proves that nested
base64 JSON exceeds the current 65,536-character ceiling at supported maximum text
size. A production compact codec must support 10,000 characters/40,000 UTF-8 bytes
and be tested against the actual schemas, device counts and batch cap. This probe
deliberately permits only **1,024 body bytes**, uses JSON integer arrays, and is not
that codec. Its labels are bounded ASCII fixture identifiers, not production UUIDs.

## 4. Group lifecycle

Use one Megolm outbound session per sending device and conversation epoch. Deliver
its session key to each currently authorized device through an authenticated Olm
leg whose kind, conversation, sender, recipient and epoch are bound above. Verify
the sending device identity and native group session ID when installing the key.

Removal/revocation requires a new sender session and redistribution to the remaining
devices before the next send; simply incrementing a metadata epoch is insufficient.
Membership must be rechecked against canonical server state, with a rule for races
between rotation and send. Old or duplicate key-distribution messages must not roll
back the installed epoch. The application persists replay uniqueness by conversation,
sender device, native session ID and message index; Megolm itself permits repeat
decrypt. Joining/rejoining gets a new authorization decision and no automatic grant
of historical keys. Existing history retained by a departed device cannot be revoked.

The earlier evaluation exercises manual multi-device key delivery, fresh-key
exclusion and index replay detection. This boundary prototype does **not** implement
group APIs, membership synchronization or durable replay storage.

## 5. Secure state and operation ordering

Proposed state namespace: authenticated account + device + generation. A native-only
random wrapping key lives in OS Keychain/Keystore under that namespace. Identity,
prekey, pairwise/group ratchet, replay and outbox state stays encrypted in native
storage. JavaScript receives public registration data, ciphertext and authorized
plaintext for rendering; it never receives private keys or pickle wrapping keys.

Lifecycle is `Locked -> Loading -> Ready -> Locked`. Loading checks the namespace,
expected identity, format/profile and stored generation before constructing handles.
Logout, account switch or revocation fences queued operations, cancels pending work,
invalidates all old handles, drops sensitive state and clears application plaintext
caches. Restore creates a fresh handle generation; old handles remain invalid. A
single serial native executor owns each account's state and transaction ordering.

Ordering required before production:

1. Send: validate trust/routing, encrypt on a candidate ratchet, atomically persist
   ratchet plus immutable ciphertext/outbox/logical ID, then release/send ciphertext.
2. Receive: decrypt on a candidate, validate all context and replay state, atomically
   persist account/prekey/ratchet/replay changes, then release plaintext and acknowledge.
3. Storage failure: discard the candidate; never expose a body or ciphertext from an
   uncommitted transition. Crash tests must exercise every boundary and retry path.

Encrypted pickles provide confidentiality/integrity, not transactionality, account
scope binding or rollback resistance. The storage record must authenticate its
namespace, profile and generation and define a rollback policy with real platform
guarantees; an unauthenticated local counter is insufficient. Missing/unrecoverable
keys mean fresh-device onboarding, consistent with SPEC's v1 fresh-history policy.
This proposal does not activate the deferred encrypted-backup feature.

Implemented here: opaque Rust handles, one-shot session transfer, a shared lock flag
that rejects further account/session/inbound-result use, and candidate-copy validation
before committing state **in memory**. Encrypted account restoration requires a
caller-trusted expected DH identity. The pickle does not bind caller-supplied
account/device labels. No disk transaction, OS key storage, queue cancellation,
cross-process rollback defense, concurrent close, or guaranteed zeroization of
already copied plaintext is proved. Test wrapping keys cross the C++ boundary only
inside this synthetic process; that is not the proposed JavaScript API.

## 6. Native packaging and release gates

CXX 1.0.202 generates the Rust/C++ bridge. C++ callers own opaque Rust `Box` values;
bounded byte vectors preserve binary data. All expected errors are fixed literals
mapped to `rust::Error`; a Rust panic at this boundary aborts, so `Result` is not a
panic shield. Production wrappers need input/fuzz/allocator-failure review and must
keep exception behavior compatible with the chosen mobile build settings.

The real host tests compile, link and execute C++ calls to Rust cryptography. A
future Expo/React Native adapter may call this native core, but no JSI/TurboModule,
Objective-C++/JNI adapter, XCFramework, Android shared library, Keychain/Keystore
binding or phone app exists here. Previous pure-Rust mobile `rlib` builds do not
prove this C++ boundary builds or links for those platforms.

Before production selection: review non-PQ/profile tradeoffs, trust resets and the
versioned directory/transport design; establish dependency notices/security review;
implement durable storage and crash tests; compile/link the actual mobile adapters;
then test two real devices, account switching, delayed delivery, restart, replay,
member leave/rejoin and revocation. This work advances c23 design evidence; it
does not close c23's phone gate or production c7/c36/c14 acceptance.

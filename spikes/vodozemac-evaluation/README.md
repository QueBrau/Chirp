# c23: vodozemac compatibility evaluation

This is an isolated, runnable evaluation of **vodozemac 0.11.0**, performed
September 21, 2026 against Chirp source `88a2179`. It uses fresh, disposable
accounts and real Olm/Megolm cryptography. It does not implement production E2EE,
change the app/backend, contact a server, read credentials, or run the historical
AGPL library. The user approved evaluation, not a production protocol selection.

Recorded local run: **15 Rust protocol/framing checks and 13 actual-schema checks
passed, with no skips**. Both ARM64 mobile targets compiled as Rust libraries;
two deliberately weakened private source copies failed the intended assertions.
The clean five-package Python environment also passed all 13 schema checks.
[RESULTS.json](RESULTS.json) records versions, aggregate measurements and source
hashes. The separate GitHub workflow is added here; its remote execution is not
part of this local evidence.

**Result: promising primitives; not a drop-in replacement.** The c339 memo's
"ZERO backend migration" claim is not established. Current byte containers accept
some encodings, but the key-directory semantics, device targeting, message
framing, trust and lifecycle contracts still need a design. This does not prove a
SQL migration is inevitable: a versioned API/client contract might reuse some
existing columns. Silently reinterpreting Signal-shaped fields is not that design.

## Reproduce

Use Rust 1.90.0 (pinned in `rust-toolchain.toml`) and the existing backend Python
environment. Dependencies and checksums are in `Cargo.lock`; nothing is installed
into `app-mobile`. Commands below run from the repository root:

```sh
cargo +1.90.0 test --locked --manifest-path spikes/vodozemac-evaluation/Cargo.toml
cargo +1.90.0 run --locked --quiet --manifest-path spikes/vodozemac-evaluation/Cargo.toml > spikes/vodozemac-evaluation/evidence.local.json
backend/.venv/bin/python spikes/vodozemac-evaluation/verify_contract.py spikes/vodozemac-evaluation/evidence.local.json
```

`cargo run` emits only public keys/signatures and synthetic ciphertext specimens.
Private keys, session exports and decrypted test bodies remain in memory. The
ignored specimen is input to the Python verifier, not a production registration.
The verifier imports the **actual current Pydantic schemas**, without starting the
app or using HTTP, Redis, SQL or credentials. Its output contains aggregate lengths
and test results. After one dependency fetch, the Cargo commands support `--offline`.

The separate `crypto-evaluation.yml` workflow runs these host/schema checks when
this spike or its schema dependencies change. It installs only the five Pydantic
packages extracted from the canonical backend hash lock, with `--require-hashes`.
It starts no database/services and does not download Rust from within pytest.
The existing application CI workflow is unchanged.

## What the executable checks establish

| Boundary | Observed behavior and implication |
| --- | --- |
| Real pairwise crypto | Prekey creation, initial decryption, reply and subsequent normal messages work. Independent recipient-device sessions refuse each other's ciphertext. Wrong sender identity and modified ciphertext fail; a failed tamper attempt does not poison a subsequent valid decrypt. |
| Signing identity | Olm has separate 32-byte Curve25519 **and** Ed25519 identities. Signatures verify with the latter, not a reinterpretation of the former. Chirp has one 32-byte `identity_key_b64` and no signing-identity field; an added field is silently dropped. Concatenating both identities is rejected. |
| Required signed prekey | Chirp requires `signed_prekey`; Olm's outbound API takes a recipient Curve25519 identity and an OTK instead. The specimen puts a real Olm fallback key and Ed25519 signature in that shape **only to demonstrate syntactic acceptance**. It does not authenticate the omitted signing identity or approve that field mapping. Correct-length invalid signatures also pass the existing schema, as its source explicitly documents. |
| Key encoding and identifiers | Native 32-byte key base64 is unpadded (43 characters), rejected by Chirp's strict decoder until padded. Olm `KeyId` is a base64-encoded `u64`; Chirp accepts bounded signed-32-bit integer identifiers. Explicit persisted mapping and rollover rules remain necessary. |
| Prekey lifecycle | Marking keys published hides the public upload list without deleting the private OTK: a delayed initial message still decrypts. Inbound creation consumes the private OTK and repeat creation fails. Native fallback keys really support repeated new sessions, but mapping Chirp's exhausted `one_time_prekey: null` and mandatory signed-prekey slot to that behavior is undecided. The server claims OTKs on bundle fetch, before delivery. |
| Framing and targeting | A small explicit JSON Olm envelope fits `ciphertext_b64`. Outer `message_type="olm"` is rejected; added recipient, algorithm and epoch fields are dropped. Chirp broadcasts the same stored row to active member users. Clients need a versioned, authenticated envelope-selection/logical-message contract, or the API must change. |
| Context | These experiments encrypt an inner context containing algorithm, version, conversation, sender device, recipient device, kind and epoch, then compare every field before returning the body. Each altered expected field fails. This is an **evaluation helper**, not native AAD or a reviewed application protocol. Olm/Megolm APIs here take plaintext, not an AAD parameter. |
| Wire subtype | Olm's prekey/normal subtypes are 0/1 and must travel in the transport. Legacy Signal subtype 2/3 is rejected. Chirp's `Conversation.protocol_version=2` is neither this subtype nor vodozemac's session configuration. |
| Group distribution | An actual Megolm session key travels inside pairwise-encrypted, context-checked messages to three device sessions (two Bob devices and one Carol device). Each can decrypt the group message. A fresh session excludes every old key. This is manual rotation, not proof of production leave-event detection or post-leave redistribution. |
| Replay/out-of-order | Olm rejects replay after successful decryption. Megolm supports out-of-order decryption and permits repeat decrypt with the same message index. Persisted duplicate detection scoped to sender/conversation/session/index is application work. The small in-memory set in the test only demonstrates the requirement. |
| State persistence | A real encrypted session pickle restores decryption and rejects a wrong pickle key. An account pickle restores the same Curve25519 identity. Constant test-only keys never leave this process. This does not prove Keychain/Keystore storage, atomic ratchet persistence, rollback defense, account isolation, reinstall or encrypted backups. |
| Current schema ceiling | For 10,000 four-byte Unicode characters (40,000 plaintext bytes), raw single-device Olm base64 measured **53,928** characters. This experiment's base64-of-JSON-with-base64-body envelope measured **72,100**, above the current **65,536** cap; a two-device envelope measured **144,220**. Both fail the actual message schema. A compact single-device framing may fit; this specific nested encoding does not. The result is not a request to raise the server limit blindly. |

One Rust test serializes the experimental envelope through padded base64/JSON,
reconstructs the native Olm message, then decrypts and checks the inner context.
This is an in-process framing roundtrip, not a production decoder or HTTP test.
The current HTTP `MessageOut` re-encodes the synthetic stored ciphertext bytes
without loss. This is schema validation, not a database/HTTP integration test.
WebSocket hints currently omit `message_type`, so a future decoder must use a
self-describing authenticated envelope or fetch canonical HTTP data. Server-created
message IDs/timestamps do not exist when the sender encrypts and cannot simply be
declared pre-send authenticated context.

Source boundaries: `backend/app/schemas/e2ee.py`, `schemas/messaging.py`,
`core/validation.py`, `routers/keys.py`, `routers/messages.py`,
`services/prekey_service.py`, and `app-mobile/src/crypto/{signal,keys,groups}.ts`.
The legacy Node spike used direct in-process group delivery and single-device
shortcuts; its success is not evidence for this candidate or the current schemas.

## Exact candidate, licensing and audit scope

- Published crate: **vodozemac 0.11.0**, September 11, 2026; source revision
  `db1b34820f3102307284e762f335b3f72c735bf0`. Downloaded crate SHA-256:
  `ba935af014ca0ae5fb468daa51da81a8a0df7daad23c052c878b7c690cdf2574`.
- Its manifest and included license declare **Apache-2.0**, satisfying the
  evaluation's exclusion of AGPL. `DEPENDENCY-LICENSES.json` records the declared
  license expressions of all 80 locked registry packages. None requires AGPL;
  expressions offering LGPL also offer MIT or Apache-2.0 alternatives. Shipping
  notices and selecting the applicable permissive alternatives remain release work.
- The spike disables default features: no legacy libolm-pickle compatibility,
  low-level API, experimental session configuration or insecure PK encryption.
  It exercises stable session configuration **V1**, including its legacy truncated
  MAC behavior. It does not select a production security profile. V2/full-MAC
  configuration is behind the crate's experimental feature, not Chirp's database
  `protocol_version` field. No PQXDH/ML-KEM is exercised; nullable Kyber storage
  does not create post-quantum protection.
- Upstream's audit statement refers to Least Authority's **March 30, 2022** report
  on specific older revisions, not 0.11.0. Its scope excludes bindings,
  dependencies and higher-level application integration. It does not certify this
  experimental framing or a future Expo module.

Primary sources checked September 21, 2026:

- [Versioned manifest](https://docs.rs/crate/vodozemac/0.11.0/source/Cargo.toml.orig),
  [included license](https://docs.rs/crate/vodozemac/0.11.0/source/LICENSE),
  [changelog](https://docs.rs/crate/vodozemac/0.11.0/source/CHANGELOG.md).
- [Olm account source](https://docs.rs/crate/vodozemac/0.11.0/source/src/olm/account/mod.rs),
  [wire messages](https://docs.rs/crate/vodozemac/0.11.0/source/src/olm/messages/mod.rs),
  [Megolm receiver](https://docs.rs/crate/vodozemac/0.11.0/source/src/megolm/inbound_group_session.rs).
- [Original audit](https://matrix.org/media/Least%20Authority%20-%20Matrix%20vodozemac%20Final%20Audit%20Report.pdf).

## Native integration boundary

This exact crate ships Rust APIs, not a standalone React Native/Expo module. There
is no Expo package, podspec, JNI/UniFFI export or foreign-language library target
in its archive. `wasm_js` selects the randomness backend; it does not generate JS
bindings. Upstream crate CI covers desktop platforms, not phone apps.

Matrix's maintained Swift/Kotlin and JS/Wasm bindings wrap **matrix-sdk** or
**matrix-sdk-crypto**, separate layers with Matrix protocol/state-machine
semantics. Importing them is not a demonstrated adapter for Chirp's API. See the
[bindings inventory](https://github.com/matrix-org/matrix-rust-sdk/blob/main/bindings/README.md),
[Apple packaging](https://github.com/matrix-org/matrix-rust-sdk/blob/main/bindings/apple/README.md)
and [Android crypto integration](https://github.com/matrix-org/matrix-rust-sdk/blob/main/bindings/matrix-sdk-crypto-ffi/README.md).

Rust target builds, when run with the commands below, only compile a Rust `rlib`.
They do not produce an Expo module, linked app, XCFramework or Android `.so`, run
on either device, or prove mobile secure storage. They use no EAS or signing:

```sh
rustup target add aarch64-apple-ios aarch64-linux-android --toolchain 1.90.0
cargo +1.90.0 build --locked --lib --target aarch64-apple-ios --manifest-path spikes/vodozemac-evaluation/Cargo.toml
cargo +1.90.0 build --locked --lib --target aarch64-linux-android --manifest-path spikes/vodozemac-evaluation/Cargo.toml
```

## What remains before production work

Choose whether Olm/Megolm's non-PQ tradeoffs and a custom mobile bridge are
acceptable. If so, separately review a versioned key directory and authenticated
device identity/trust model, bounded per-device transport and logical-message
framing, group membership/rotation and replay lifecycle, and durable secure state.
Then build the native wrapper and test two actual devices, cold starts, interrupted
writes, device changes, leave/rejoin and delayed messages. No c7/c36/c14 production
acceptance or c23 two-phone gate is closed by this spike.

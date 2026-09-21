# c23: real C++ / Rust crypto boundary

This isolated follow-up to the [compatibility evaluation](../vodozemac-evaluation/README.md)
implements a small **CXX 1.0.202** bridge around **vodozemac 0.11.0**, using real Olm
and Ed25519 operations. C++ creates accounts, verifies public bindings, establishes
sessions, encrypts/decrypts binary data, locks handles and restores an encrypted
account. The Rust test runner calls the compiled C++ probes, which call back into
Rust through generated bindings. This is an executed native-language boundary.

It is **not an Expo module or a production security selection**. No application,
backend, schema, mobile dependency, composer, server or cloud configuration changes.
No network, credentials or external services are used by the executable. The
[proposed contract](PROPOSED-CONTRACT.md) specifies the directory, device trust,
per-device transport, group lifecycle and secure-state work still required.

## Reproduce

Requires Rust 1.90.0 and a host C++17 compiler with exceptions enabled. Versions and
registry checksums are pinned in `Cargo.lock`; the toolchain is pinned separately.
From the repository root:

```sh
CARGO_BUILD_JOBS=2 cargo +1.90.0 test --locked --manifest-path spikes/vodozemac-native-boundary/Cargo.toml
CARGO_BUILD_JOBS=2 cargo +1.90.0 run --locked --quiet --manifest-path spikes/vodozemac-native-boundary/Cargo.toml
```

After the initial public dependency fetch, both commands accept `--offline`.
The binary prints only an aggregate JSON result. Keys, signatures, ciphertext,
pickles and decrypted fixture bytes are not printed. Fixed error strings cross
the bridge; upstream error details and caller input do not appear in diagnostics.
All accounts and constant test-only wrapping keys are disposable in-memory data.

The separate `crypto-native-boundary.yml` workflow performs these two checks on
Ubuntu when this spike or that workflow changes. It has read-only repository
permissions, two Cargo jobs, a 15-minute deadline, and no secrets or services.
No backend test or mobile build is needed for this isolated experiment.

## Observed results

Local September 21, 2026: **9 tests passed, no skips**, plus the host executable.
Rust 1.90.0 and Apple clang 14.0.0 compiled, linked and executed the boundary on
Intel macOS. Three deliberately weakened private copies failed the intended
tests; the original source passed again. [RESULTS.json](RESULTS.json) records
aggregate evidence. A remote CI result is separate evidence, not claimed here.

| C++-initiated case | What it proves within this process |
| --- | --- |
| Prekey, reply, normal message | Genuine Olm handshake and roundtrip; byte vectors preserve NUL and non-UTF8 data, and the post-reply subtype is normal (1). |
| Signed identity binding | Real Ed25519 verification rejects changed account/device/DH/signature bytes. It does not establish who owns an account, authenticate the test OTK or implement a trusted directory. |
| Wrong recipient | An independently generated recipient with identical public labels still cannot decrypt. Wrong recipient-device context fails; the proper recipient can decrypt afterward. |
| Tamper | Modified ciphertext fails through a fixed C++ error; the valid message still decrypts. |
| Context / state ordering | Wrong conversation, logical message ID, content kind or epoch fails. An initial context failure does not consume the live OTK; a normal failure does not advance the live ratchet. Candidate copies commit only after validation, in memory. |
| Versions, types, bounds | Unsupported context version/kind, oversized labels/body, invalid subtype and empty ciphertext fail closed. This is a 1,024-byte fixture body cap, not Chirp's production text limit or final wire codec. |
| Lock | Locking the account rejects further account, existing session and retained inbound-result operations, without locking another account. Previously copied plaintext is not erased; no cross-thread or queued-operation claim. |
| Encrypted account restore | Wrong wrapping key or expected DH identity fails. A restored account retains its identity and real private prekey needed to decrypt a new initial message. Trusted account/device scope is supplied by the caller, not authenticated by this pickle. |
| Replay / ownership | A repeated Olm decrypt fails; the opaque inbound session can be moved out only once. No durable replay database or crash recovery is implemented. |

The negative mutations remove context comparison, commit the consumed OTK before
context validation, or remove the existing-session lock check. Each compiled and
failed the corresponding behavioral assertion; these were not compile-error-only
probes. They are private evidence, not alternate implementations committed here.

## Native boundary and remaining limits

`cxx::bridge` defines opaque Rust ownership, explicit byte vectors and fallible
calls; generated code supplies the ABI and memory plumbing. `src/probe.cc` is a
caller/test harness, not a hand-written cryptographic implementation. The inner
context framing and signed identity tuple are evaluation-only application codecs.
The production proposal has additional fields and requirements; it cannot copy
these fixture structs unchanged.

The account lock uses a shared flag. It does not promise secure erasure of copies
or secrets from RAM. Ratchet/prekey transitions are transactional only in memory.
Snapshot code exercises encrypted upstream pickles, not Keychain/Keystore, disk
atomicity, label authentication, rollback resistance or backup recovery. The
prototype trusts supplied public OTKs and has no directory/network authorization.

Vodozemac's stable session configuration V1 is used without default/experimental
features. It is non-PQ and has legacy truncated MAC behavior. The prior upstream
audit covers older library revisions, not this bridge/application protocol. The
[proposal](PROPOSED-CONTRACT.md) retains explicit security and production gates.

No iOS/Android C++ build is claimed. Local `xcrun --sdk iphoneos --show-sdk-path`
failed because the iPhoneOS SDK is absent; no SDK or paid build was installed.
Android native packaging was not attempted. Earlier pure-Rust mobile `rlib`
success does not prove the new C++ bridge compiles/links on those targets. No JSI,
TurboModule, Objective-C++/JNI adapter, app binary or two-phone execution exists.

## Source and license provenance

The downloaded CXX and cxx-build 1.0.202 manifests declare `MIT OR Apache-2.0`,
Rust 1.88 minimum, and the upstream dtolnay/cxx repository. Rust 1.90.0 satisfies
that requirement. Vodozemac 0.11.0 remains Apache-2.0; the earlier evaluation
records its source revision and crate hash. No AGPL library is introduced.

[DEPENDENCY-LICENSES.json](DEPENDENCY-LICENSES.json) records declared expressions
and included license-file hashes for the 90 registry packages returned by locked
host-filtered Cargo metadata. Those entries have no AGPL requirement. This is
host provenance, not a complete target-independent legal review or shipping-notice
package; future mobile dependency features and artifacts require their own audit.

Primary sources checked September 21, 2026 (downloaded pinned crate sources were
also inspected locally):

- [CXX release metadata](https://docs.rs/crate/cxx/1.0.202),
  [versioned manifest](https://docs.rs/crate/cxx/1.0.202/source/Cargo.toml.orig),
  [CXX build tutorial](https://cxx.rs/tutorial.html).
- [Opaque Rust ownership](https://cxx.rs/extern-rust.html),
  [supported shared types](https://cxx.rs/shared.html),
  [fallible calls and panic behavior](https://cxx.rs/binding/result.html).
  CXX maps Rust `Result` to C++ exceptions; a Rust panic aborts instead of becoming
  a recoverable error. Production fuzzing/input review must account for this.
- [Vodozemac account APIs](https://docs.rs/vodozemac/0.11.0/vodozemac/olm/struct.Account.html),
  [session APIs](https://docs.rs/vodozemac/0.11.0/vodozemac/olm/struct.Session.html),
  [versioned source and license](https://docs.rs/crate/vodozemac/0.11.0/source/).

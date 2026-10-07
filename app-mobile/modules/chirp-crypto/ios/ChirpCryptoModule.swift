import ExpoModulesCore
import Foundation

/// The iOS side of the E2EE core (board c448, design sections 2 and 3).
///
/// This file owns what Rust cannot: the Keychain, the store directory, and the handle
/// table. Everything cryptographic is in the Rust core behind the uniffi bindings in
/// Generated/. The rules it keeps:
///
///  - ONE serial queue runs every call. The core serializes operations on a handle with a
///    mutex, but a mutex gives exclusion, not order; the queue is what makes the order of
///    sends and receives deterministic. The handle table is only touched on that queue.
///  - The store key never leaves this process's native side: it is read from or created in
///    the Keychain and handed straight to Rust. JavaScript only sees opaque handle ids.
///  - Errors reject with a fixed code and nothing else (ChirpCryptoException).
///  - Binary values are base64 text and ids are UUID text; Rust parses and emits them.
public final class ChirpCryptoModule: Module {
  /// The single serial queue for every call.
  private let queue = DispatchQueue(label: "app.chirps.mobile.e2ee.core", qos: .userInitiated)

  /// One entry per open handle. Queue-confined.
  private var handles: [String: OpenHandle] = [:]

  /// An unlocked namespace. `pinned` is the last generation written to the Keychain, so
  /// an unchanged store does not cost a Keychain write after every read-only-ish call.
  private final class OpenHandle {
    let core: CryptoCore
    let label: String
    var pinned: UInt64?

    init(core: CryptoCore, label: String, pinned: UInt64?) {
      self.core = core
      self.label = label
      self.pinned = pinned
    }
  }

  public func definition() -> ModuleDefinition {
    Name("ChirpCrypto")

    // A JS reload or app teardown: close every store so the next run is not met with
    // `store_in_use`. Dropping the objects would close them too; this makes it explicit.
    OnDestroy {
      self.queue.sync {
        for (_, handle) in self.handles {
          handle.core.lock()
        }
        self.handles.removeAll()
      }
    }

    // MARK: Lifecycle

    AsyncFunction("unlock") { (userId: String, generation: Int) -> String in
      try self.guarded { try self.unlock(userId: userId, generation: generation) }
    }.runOnQueue(queue)

    AsyncFunction("lock") { (handle: String) in
      try self.guarded {
        if let entry = self.handles.removeValue(forKey: handle) {
          entry.core.lock()
        }
      }
    }.runOnQueue(queue)

    AsyncFunction("wipe") { (userId: String, generation: Int) in
      try self.guarded { try self.wipe(userId: userId, generation: generation) }
    }.runOnQueue(queue)

    // MARK: Device and keys

    AsyncFunction("createDevice") { (handle: String, otkCount: Int) -> [String: Any] in
      try self.guarded {
        let count = try UInt32(exactly: otkCount).orInvalid()
        return js(try self.call(handle, mutating: true) { try $0.createDevice(otkCount: count) })
      }
    }.runOnQueue(queue)

    AsyncFunction("markPublished") { (handle: String, serverDeviceId: String, ids: [Int]) in
      try self.guarded {
        let keys = try keyIds(ids)
        try self.call(handle, mutating: true) {
          try $0.markPublished(serverDeviceId: serverDeviceId, keyIds: keys)
        }
      }
    }.runOnQueue(queue)

    AsyncFunction("topUpKeys") { (handle: String, count: Int) -> [[String: Any]] in
      try self.guarded {
        let wanted = try UInt32(exactly: count).orInvalid()
        let keys = try self.call(handle, mutating: true) { try $0.topUpKeys(count: wanted) }
        return keys.map { js($0) }
      }
    }.runOnQueue(queue)

    AsyncFunction("pendingKeys") { (handle: String) -> [[String: Any]] in
      try self.guarded {
        try self.call(handle) { try $0.pendingKeys() }.map { js($0) }
      }
    }.runOnQueue(queue)

    AsyncFunction("rotateFallbackKey") { (handle: String) -> [String: Any] in
      try self.guarded {
        js(try self.call(handle, mutating: true) { try $0.rotateFallbackKey() })
      }
    }.runOnQueue(queue)

    AsyncFunction("keyStatus") { (handle: String) -> [String: Any] in
      try self.guarded { js(try self.call(handle) { try $0.keyStatus() }) }
    }.runOnQueue(queue)

    AsyncFunction("publicIdentity") { (handle: String) -> [String: Any]? in
      try self.guarded {
        try self.call(handle) { try $0.publicIdentity() }.map { js($0) }
      }
    }.runOnQueue(queue)

    // MARK: Trust

    AsyncFunction("observeDirectory") {
      (handle: String, userId: String, devices: [DirectoryDeviceArg]) -> String in
      try self.guarded {
        let entries = try devices.map { try $0.toCore() }
        let state = try self.call(handle, mutating: true) {
          try $0.observeDirectory(userId: userId, devices: entries)
        }
        return jsEnum(state)
      }
    }.runOnQueue(queue)

    AsyncFunction("trustState") { (handle: String, peerUserId: String) -> String in
      try self.guarded {
        jsEnum(try self.call(handle) { try $0.trustState(peerUserId: peerUserId) })
      }
    }.runOnQueue(queue)

    AsyncFunction("acceptIdentityChange") { (handle: String, peerUserId: String) in
      try self.guarded {
        try self.call(handle, mutating: true) { try $0.acceptIdentityChange(peerUserId: peerUserId) }
      }
    }.runOnQueue(queue)

    AsyncFunction("approveDevice") { (handle: String, newDevice: NewDeviceBindingArg) -> [String: Any] in
      try self.guarded {
        let binding = try newDevice.toCore()
        return js(try self.call(handle) { try $0.approveDevice(newDevice: binding) })
      }
    }.runOnQueue(queue)

    AsyncFunction("safetyNumber") {
      (myUserId: String, myIdentities: [String], peerUserId: String, peerIdentities: [String]) -> String in
      try self.guarded {
        try safetyNumber(
          myUserId: myUserId, myIdentities: myIdentities, peerUserId: peerUserId,
          peerIdentities: peerIdentities)
      }
    }.runOnQueue(queue)

    // MARK: Messages

    AsyncFunction("encrypt") {
      (handle: String, conversationId: String, clientMessageId: String, body: String,
       sender: DirectoryDeviceArg, recipients: [RecipientArg]) -> [[String: Any]] in
      try self.guarded {
        let me = try sender.toCore()
        let to = try recipients.map { try $0.toCore() }
        let legs = try self.call(handle, mutating: true) {
          try $0.encrypt(
            conversationId: conversationId, clientMessageId: clientMessageId, body: body,
            sender: me, recipients: to)
        }
        return legs.map { js($0) }
      }
    }.runOnQueue(queue)

    AsyncFunction("decrypt") {
      (handle: String, messageId: String, leg: LegArg, sender: DirectoryDeviceArg,
       expected: ExpectedEnvelopeArg) -> [String: Any] in
      try self.guarded {
        let theLeg = try leg.toCore()
        let from = try sender.toCore()
        let routing = expected.toCore()
        let message = try self.call(handle, mutating: true) {
          try $0.decrypt(messageId: messageId, leg: theLeg, sender: from, expected: routing)
        }
        return js(message)
      }
    }.runOnQueue(queue)

    AsyncFunction("listMessages") {
      (handle: String, conversationId: String, before: Int?, limit: Int) -> [[String: Any]] in
      try self.guarded {
        let cursor = try before.map { try UInt64(exactly: $0).orInvalid() }
        let page = try UInt32(exactly: limit).orInvalid()
        let rows = try self.call(handle) {
          try $0.listMessages(conversationId: conversationId, before: cursor, limit: page)
        }
        return rows.map { js($0) }
      }
    }.runOnQueue(queue)
  }

  // MARK: - Error funnel

  /// Run a call body and turn anything it throws into exactly one ChirpCryptoException.
  private func guarded<T>(_ body: () throws -> T) throws -> T {
    do {
      return try body()
    } catch {
      throw chirpCryptoError(error)
    }
  }

  // MARK: - Handles

  /// Run `body` on an open handle. After a successful MUTATING call the store generation
  /// is pinned in the Keychain.
  ///
  /// Order matters, and it is the whole point of the pin: the Rust call has already
  /// COMMITTED its transaction when it returns, and only then do we read the new
  /// generation and write it to the Keychain. The pin is never written before the commit.
  /// So a crash between the commit and the pin leaves pin <= store, which `unlock`
  /// accepts (it refuses only store < pin). Writing the pin first would leave pin > store
  /// after such a crash and lock the user out of their own history.
  ///
  /// A failure to write the pin does NOT fail the call: the commit already happened and
  /// its result (a decrypted message, a new device's keys) may not be recoverable by
  /// retrying. It is the same state as a crash before the pin, so it is safe by the
  /// argument above, and the next successful write or `unlock` catches the pin up.
  private func call<T>(
    _ handleId: String, mutating: Bool = false, _ body: (CryptoCore) throws -> T
  ) throws -> T {
    guard let entry = handles[handleId] else {
      // A handle that was locked, wiped, or never existed is dead, same as in Rust.
      throw ChirpCryptoException("locked")
    }
    let result = try body(entry.core)
    if mutating {
      pinCurrentGeneration(entry)
    }
    return result
  }

  private func pinCurrentGeneration(_ entry: OpenHandle) {
    guard let current = try? entry.core.storeGeneration() else { return }
    if let pinned = entry.pinned, pinned >= current { return }
    do {
      try ChirpKeychain.writePin(entry.label, current)
      entry.pinned = current
    } catch {
      // See `call`: deliberately not fatal.
    }
  }

  // MARK: - unlock / wipe

  private func unlock(userId: String, generation: Int) throws -> String {
    let gen = try UInt32(exactly: generation).orInvalid()
    // Validates the user id (canonical UUID) before it names anything in the Keychain.
    let label = try namespaceLabel(userId: userId, generation: gen)
    let directory = try storeDirectory()
    let exists = try storeExists(dir: directory.path, userId: userId, generation: gen)

    var key: Data
    var pin: UInt64?
    if exists {
      // An existing store opens only with its own key. If the Keychain no longer has it
      // (restore to a new device, Keychain reset) it can never be opened: report that and
      // change nothing. A fresh key here would only make the failure harder to see.
      guard let existing = try ChirpKeychain.readStoreKey(label) else {
        throw ChirpCryptoException("unlock_failed")
      }
      key = existing
      pin = try ChirpKeychain.readPin(label)
    } else {
      // A new namespace. iOS keeps Keychain items across an app delete and reinstall, so
      // a leftover key and pin from a previous install are garbage with no store behind
      // them. Clear them before making a fresh key, or the stale pin would refuse the
      // brand new store as "rolled back" forever. This does not weaken the check: a
      // rollback attack is an OLDER store appearing (the file exists), never a missing one.
      try ChirpKeychain.deleteBoth(label)
      key = try ChirpKeychain.createStoreKey(label)
      pin = nil
    }
    defer { key.resetBytes(in: 0..<key.count) }  // best effort; Data may have been copied

    let core = try CryptoCore.unlock(
      dir: directory.path, userId: userId, generation: gen, storeKey: key, pinnedGeneration: pin)
    let entry = OpenHandle(core: core, label: label, pinned: pin)
    let id = UUID().uuidString.lowercased()
    handles[id] = entry
    // The store may have just been created, or may be ahead of a lagging pin.
    pinCurrentGeneration(entry)
    return id
  }

  private func wipe(userId: String, generation: Int) throws {
    let gen = try UInt32(exactly: generation).orInvalid()
    let label = try namespaceLabel(userId: userId, generation: gen)
    // Close any open handle for this namespace first; deleting the files under a live
    // SQLite connection would leave it writing to an unlinked file.
    for (id, entry) in handles where entry.label == label {
      entry.core.lock()
      handles.removeValue(forKey: id)
    }
    let directory = try storeDirectory()
    // The key goes first: once it is gone the store is unreadable even if the file
    // removal below fails or the app is killed in between. Everything here is idempotent,
    // so a retry after a failure finishes the job.
    try ChirpKeychain.deleteBoth(label)
    try wipeNamespace(dir: directory.path, userId: userId, generation: gen)
  }

  // MARK: - Store directory

  /// `Application Support/e2ee/`, owner-only, excluded from iCloud and device backups, and
  /// in the same data-protection class as the Keychain items (readable after the first
  /// unlock, so a push-triggered wake can still decrypt). The exclusion flag is re-applied
  /// and read back on every unlock, so a directory that lost it is repaired and a failure to
  /// set it is an error rather than a silent backup of ciphertext.
  private func storeDirectory() throws -> URL {
    let fileManager = FileManager.default
    do {
      let support = try fileManager.url(
        for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
      var directory = support.appendingPathComponent("e2ee", isDirectory: true)
      if !fileManager.fileExists(atPath: directory.path) {
        try fileManager.createDirectory(
          at: directory, withIntermediateDirectories: true,
          attributes: [
            .posixPermissions: 0o700,
            .protectionKey: FileProtectionType.completeUntilFirstUserAuthentication,
          ])
      }
      var values = URLResourceValues()
      values.isExcludedFromBackup = true
      try directory.setResourceValues(values)
      let readBack = try directory.resourceValues(forKeys: [.isExcludedFromBackupKey])
      guard readBack.isExcludedFromBackup == true else {
        throw ChirpCryptoException("storage_failed")
      }
      return directory
    } catch {
      throw ChirpCryptoException("storage_failed")
    }
  }
}

private extension Optional {
  /// Unwrap a numeric conversion that must succeed, else `invalid_input`.
  func orInvalid() throws -> Wrapped {
    guard let value = self else { throw ChirpCryptoException("invalid_input") }
    return value
  }
}

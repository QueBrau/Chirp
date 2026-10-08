import Foundation
import Security

/// The two Keychain items per namespace (board c448, design section 3):
///
///  - the STORE KEY: 32 random bytes that encrypt the Rust store. Generated here, held
///    here, handed to the Rust core at unlock, NEVER returned to JavaScript.
///  - the GENERATION PIN: the store's commit counter as last seen after a successful
///    mutating call, to detect an older copy of the store being restored.
///
/// Both are generic passwords. The service names are fixed, the account is the
/// namespace label (`<32 hex of the user id>-g<generation>`, built by Rust from a
/// validated UUID), the accessibility is `AfterFirstUnlockThisDeviceOnly` (usable by a
/// push-triggered wake after the first unlock, never in a backup or on another device),
/// and `kSecAttrSynchronizable` is false so iCloud Keychain never carries them.
internal enum ChirpKeychain {
  static let storeKeyService = "app.chirps.mobile.e2ee.storekey"
  static let storeGenService = "app.chirps.mobile.e2ee.storegen"

  static let storeKeyLength = 32

  private static let accessible = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly

  private static func query(_ service: String, _ account: String) -> [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: account,
      kSecAttrSynchronizable as String: kCFBooleanFalse as Any,
    ]
  }

  /// nil when there is no such item; throws `keychain_failed` on any other failure.
  static func read(_ service: String, _ account: String) throws -> Data? {
    var request = query(service, account)
    request[kSecReturnData as String] = true
    request[kSecMatchLimit as String] = kSecMatchLimitOne
    var item: CFTypeRef?
    let status = SecItemCopyMatching(request as CFDictionary, &item)
    if status == errSecItemNotFound {
      return nil
    }
    guard status == errSecSuccess, let data = item as? Data else {
      throw ChirpCryptoException("keychain_failed")
    }
    return data
  }

  /// Create or replace the item's value.
  static func write(_ service: String, _ account: String, _ data: Data) throws {
    var add = query(service, account)
    add[kSecValueData as String] = data
    add[kSecAttrAccessible as String] = accessible
    var status = SecItemAdd(add as CFDictionary, nil)
    if status == errSecDuplicateItem {
      let change: [String: Any] = [
        kSecValueData as String: data,
        kSecAttrAccessible as String: accessible,
      ]
      status = SecItemUpdate(query(service, account) as CFDictionary, change as CFDictionary)
    }
    guard status == errSecSuccess else {
      throw ChirpCryptoException("keychain_failed")
    }
  }

  /// Deleting an item that is not there succeeds.
  static func delete(_ service: String, _ account: String) throws {
    let status = SecItemDelete(query(service, account) as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
      throw ChirpCryptoException("keychain_failed")
    }
  }

  // MARK: - Store key

  static func readStoreKey(_ account: String) throws -> Data? {
    guard let key = try read(storeKeyService, account) else {
      return nil
    }
    // A key of the wrong length is not something we wrote. Do not guess: refuse.
    guard key.count == storeKeyLength else {
      throw ChirpCryptoException("keychain_failed")
    }
    return key
  }

  /// Generate a fresh 32-byte key from the system CSPRNG and store it.
  static func createStoreKey(_ account: String) throws -> Data {
    var bytes = [UInt8](repeating: 0, count: storeKeyLength)
    let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    guard status == errSecSuccess else {
      throw ChirpCryptoException("internal")
    }
    defer { bytes.withUnsafeMutableBytes { _ = memset_s($0.baseAddress, $0.count, 0, $0.count) } }
    let key = Data(bytes)
    try write(storeKeyService, account, key)
    return key
  }

  // MARK: - Generation pin

  static func readPin(_ account: String) throws -> UInt64? {
    guard let data = try read(storeGenService, account) else {
      return nil
    }
    // Eight bytes, big endian. Anything else is not our value: refuse to guess.
    guard data.count == 8 else {
      throw ChirpCryptoException("keychain_failed")
    }
    return data.reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
  }

  static func writePin(_ account: String, _ generation: UInt64) throws {
    var big = generation.bigEndian
    let data = withUnsafeBytes(of: &big) { Data($0) }
    try write(storeGenService, account, data)
  }

  static func deleteBoth(_ account: String) throws {
    try delete(storeKeyService, account)
    try delete(storeGenService, account)
  }
}

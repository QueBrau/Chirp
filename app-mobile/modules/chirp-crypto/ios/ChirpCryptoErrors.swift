import ExpoModulesCore

/// Every failure that reaches JavaScript is one of the Rust core's fixed code strings
/// (`locked`, `decrypt_failed`, ...) plus the two raised at the platform boundary
/// (`store_rolled_back`, `keychain_failed`). The code is the ONLY thing carried: no
/// payload, no underlying error text, no plaintext, no key material. The JS error's
/// `code` is this string (Expo copies a thrown Exception's code onto the rejection).
internal final class ChirpCryptoException: Exception, @unchecked Sendable {
  private let fixedCode: String

  init(_ code: String) {
    let safe = ChirpCryptoException.sanitized(code)
    self.fixedCode = safe
    super.init(name: "ChirpCryptoException", description: safe, code: safe)
  }

  override var reason: String {
    fixedCode
  }

  /// A code is lowercase words joined by underscores. Anything else - which would mean a
  /// bug somewhere upstream, and might carry text we did not intend to expose - collapses
  /// to `internal`.
  private static func sanitized(_ code: String) -> String {
    let allowed = code.count > 0 && code.count <= 40
      && code.unicodeScalars.allSatisfy { ($0.value >= 97 && $0.value <= 122) || $0.value == 95 }
    return allowed ? code : "internal"
  }
}

extension CryptoError {
  /// The stable code string. The Rust enum's Display text is the code (a host test pins
  /// every one), and uniffi hands it over as the case's `message`. The switch is exhaustive
  /// on purpose: a case added in Rust fails to compile here until it is handled.
  var code: String {
    switch self {
    case .Locked(let message), .UnlockFailed(let message), .StoreInUse(let message),
      .Storage(let message), .StoreCorrupt(let message), .NamespaceMismatch(let message),
      .DeviceExists(let message), .NoDevice(let message), .NotPublished(let message),
      .InvalidInput(let message), .BodyInvalid(let message), .BodyTooLarge(let message),
      .InvalidSignature(let message), .InvalidKey(let message), .UnknownKey(let message),
      .TooManyKeys(let message), .SenderMismatch(let message),
      .MissingClaimedKey(let message), .TrustHardStop(let message), .UnknownPeer(let message),
      .NothingToAccept(let message), .ClientMessageIdReused(let message),
      .CiphertextTooLarge(let message), .EncryptFailed(let message),
      .DecryptFailed(let message), .InvalidEnvelope(let message),
      .EnvelopeMismatch(let message), .Replay(let message), .Internal(let message),
      .StoreRolledBack(let message), .KeychainFailed(let message):
      return message
    }
  }
}

/// Anything thrown inside a module call becomes exactly one ChirpCryptoException.
/// A panic inside Rust or an uniffi internal error carries a message we do not forward.
internal func chirpCryptoError(_ error: Error) -> Exception {
  if let known = error as? ChirpCryptoException {
    return known
  }
  if let rust = error as? CryptoError {
    return ChirpCryptoException(rust.code)
  }
  return ChirpCryptoException("internal")
}

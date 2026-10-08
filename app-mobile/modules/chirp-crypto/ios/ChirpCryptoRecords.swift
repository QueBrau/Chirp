import ExpoModulesCore

// The JavaScript <-> Swift shapes. Everything is plain strings and numbers:
//  - ids are lowercase hyphenated UUID text,
//  - binary values are base64 text (the Rust layer parses and emits them),
//  - enums are snake_case strings.
// Inputs arrive as Expo Records and are converted to the uniffi types; outputs are
// converted to dictionaries. Nothing here logs or formats a value.

private func invalidInput() -> ChirpCryptoException {
  ChirpCryptoException("invalid_input")
}

/// A JS number that must be a non-negative integer.
private func unsigned64(_ value: Int) throws -> UInt64 {
  guard let converted = UInt64(exactly: value) else { throw invalidInput() }
  return converted
}

private func unsigned32(_ value: Int) throws -> UInt32 {
  guard let converted = UInt32(exactly: value) else { throw invalidInput() }
  return converted
}

// MARK: - Input records

internal struct SignedKeyArg: Record {
  @Field var keyId: Int = 0
  @Field var kind: String = ""
  @Field var publicKey: String = ""
  @Field var signature: String = ""

  func toCore() throws -> SignedKey {
    let parsed: KeyKind
    switch kind {
    case "one_time": parsed = .oneTime
    case "fallback": parsed = .fallback
    default: throw invalidInput()
    }
    return SignedKey(
      keyId: try unsigned64(keyId), kind: parsed, publicKey: publicKey, signature: signature)
  }
}

internal struct DeviceApprovalArg: Record {
  @Field var approverEd25519: String = ""
  @Field var signature: String = ""

  func toCore() -> DeviceApproval {
    DeviceApproval(approverEd25519: approverEd25519, signature: signature)
  }
}

internal struct DirectoryDeviceArg: Record {
  @Field var userId: String = ""
  @Field var deviceId: String = ""
  @Field var generation: Int = 0
  @Field var identityCurve25519: String = ""
  @Field var identityEd25519: String = ""
  @Field var bindingSignature: String = ""
  @Field var approval: DeviceApprovalArg?

  func toCore() throws -> DirectoryDevice {
    DirectoryDevice(
      userId: userId, deviceId: deviceId, generation: try unsigned32(generation),
      identityCurve25519: identityCurve25519, identityEd25519: identityEd25519,
      bindingSignature: bindingSignature, approval: approval?.toCore())
  }
}

internal struct NewDeviceBindingArg: Record {
  @Field var userId: String = ""
  @Field var generation: Int = 0
  @Field var identityCurve25519: String = ""
  @Field var identityEd25519: String = ""
  @Field var bindingSignature: String = ""

  func toCore() throws -> NewDeviceBinding {
    NewDeviceBinding(
      userId: userId, generation: try unsigned32(generation),
      identityCurve25519: identityCurve25519, identityEd25519: identityEd25519,
      bindingSignature: bindingSignature)
  }
}

internal struct RecipientArg: Record {
  @Field var device: DirectoryDeviceArg = DirectoryDeviceArg()
  @Field var claimedKey: SignedKeyArg?

  func toCore() throws -> Recipient {
    Recipient(device: try device.toCore(), claimedKey: try claimedKey?.toCore())
  }
}

internal struct LegArg: Record {
  @Field var recipientDeviceId: String = ""
  @Field var olmType: Int = 0
  @Field var ciphertext: String = ""

  func toCore() throws -> Leg {
    guard let type = UInt8(exactly: olmType) else { throw invalidInput() }
    return Leg(recipientDeviceId: recipientDeviceId, olmType: type, ciphertext: ciphertext)
  }
}

internal struct ExpectedEnvelopeArg: Record {
  @Field var conversationId: String = ""
  @Field var clientMessageId: String = ""
  @Field var senderUserId: String = ""
  @Field var senderDeviceId: String = ""
  @Field var recipientUserId: String = ""
  @Field var recipientDeviceId: String = ""

  func toCore() -> ExpectedEnvelope {
    ExpectedEnvelope(
      conversationId: conversationId, clientMessageId: clientMessageId,
      senderUserId: senderUserId, senderDeviceId: senderDeviceId,
      recipientUserId: recipientUserId, recipientDeviceId: recipientDeviceId)
  }
}

internal func keyIds(_ values: [Int]) throws -> [UInt64] {
  try values.map(unsigned64)
}

// MARK: - Output dictionaries

internal func jsNumber(_ value: UInt64) -> Int {
  // Counters (key ids, message seq, timestamps) are far below 2^53 in any real store.
  Int(clamping: value)
}

internal func jsEnum(_ kind: KeyKind) -> String {
  switch kind {
  case .oneTime: return "one_time"
  case .fallback: return "fallback"
  }
}

internal func jsEnum(_ state: TrustState) -> String {
  switch state {
  case .trusted: return "trusted"
  case .identityChanged: return "identity_changed"
  case .unapprovedDevice: return "unapproved_device"
  }
}

internal func jsEnum(_ direction: Direction) -> String {
  switch direction {
  case .incoming: return "incoming"
  case .outgoing: return "outgoing"
  }
}

internal func js(_ identity: PublicIdentity) -> [String: Any] {
  ["curve25519": identity.curve25519, "ed25519": identity.ed25519]
}

internal func js(_ key: SignedKey) -> [String: Any] {
  [
    "keyId": jsNumber(key.keyId), "kind": jsEnum(key.kind), "publicKey": key.publicKey,
    "signature": key.signature,
  ]
}

internal func js(_ device: CreatedDevice) -> [String: Any] {
  [
    "identity": js(device.identity),
    "bindingSignature": device.bindingSignature,
    "oneTimeKeys": device.oneTimeKeys.map { js($0) },
    "fallbackKey": js(device.fallbackKey),
  ]
}

internal func js(_ approval: DeviceApproval) -> [String: Any] {
  ["approverEd25519": approval.approverEd25519, "signature": approval.signature]
}

internal func js(_ leg: Leg) -> [String: Any] {
  [
    "recipientDeviceId": leg.recipientDeviceId, "olmType": Int(leg.olmType),
    "ciphertext": leg.ciphertext,
  ]
}

internal func js(_ status: KeyStatus) -> [String: Any] {
  [
    "pending": Int(status.pending), "publishedOneTime": Int(status.publishedOneTime),
    "hasFallback": status.hasFallback,
  ]
}

internal func js(_ message: PlaintextMessage) -> [String: Any] {
  var out: [String: Any] = [
    "seq": jsNumber(message.seq),
    "direction": jsEnum(message.direction),
    "conversationId": message.conversationId,
    "clientMessageId": message.clientMessageId,
    "senderUserId": message.senderUserId,
    "senderDeviceId": message.senderDeviceId,
    "kind": message.kind,
    "body": message.body,
    "senderTrust": jsEnum(message.senderTrust),
    "storedAtMs": jsNumber(message.storedAtMs),
  ]
  // A message this device sent has no server id yet.
  out["messageId"] = message.messageId ?? NSNull()
  return out
}

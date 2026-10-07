//! The inner envelope: versioned JSON that lives INSIDE the Olm plaintext, so every
//! field is authenticated by the session. Design section 6.
//!
//! ```json
//! {"v":1,"conversation_id":"..","client_message_id":"..",
//!  "sender":{"user_id":"..","device_id":".."},
//!  "recipient":{"user_id":"..","device_id":".."},
//!  "kind":"text","body":".."}
//! ```
//!
//! UUIDs are canonical lowercase hyphenated text; anything else is rejected, so a
//! single value cannot have two spellings. Unknown fields are rejected.

use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

use crate::error::{Error, Result};
use crate::types::{ClientMessageId, ConversationId, DeviceId, UserId};

pub const ENVELOPE_VERSION: u32 = 1;
pub const KIND_TEXT: &str = "text";

/// Product limits (design section 5): 10,000 characters, at most 40,000 UTF-8 bytes.
pub const MAX_BODY_CHARS: usize = 10_000;
pub const MAX_BODY_BYTES: usize = 40_000;
/// Hard cap on a decrypted plaintext before it is even parsed.
pub const MAX_ENVELOPE_BYTES: usize = 48 * 1024;
/// The server's per-leg limit on base64 characters.
pub const MAX_LEG_B64_LEN: usize = 65_536;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Party {
    user_id: String,
    device_id: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Wire {
    v: u32,
    conversation_id: String,
    client_message_id: String,
    sender: Party,
    recipient: Party,
    kind: String,
    body: String,
}

/// A parsed envelope with typed fields.
pub struct Envelope {
    pub conversation_id: ConversationId,
    pub client_message_id: ClientMessageId,
    pub sender_user_id: UserId,
    pub sender_device_id: DeviceId,
    pub recipient_user_id: UserId,
    pub recipient_device_id: DeviceId,
    pub body: String,
}

pub fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 15) as usize] as char);
    }
    out
}

fn nibble(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        _ => None,
    }
}

/// Lowercase hex only; uppercase is rejected on purpose.
pub fn unhex<const N: usize>(text: &str) -> Option<[u8; N]> {
    let bytes = text.as_bytes();
    if bytes.len() != N * 2 {
        return None;
    }
    let mut out = [0u8; N];
    for (i, pair) in bytes.chunks_exact(2).enumerate() {
        out[i] = (nibble(pair[0])? << 4) | nibble(pair[1])?;
    }
    Some(out)
}

/// Canonical lowercase hyphenated form: 8-4-4-4-12.
pub fn uuid_to_text(id: &[u8; 16]) -> String {
    let h = hex(id);
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

pub fn uuid_from_text(text: &str) -> Option<[u8; 16]> {
    if text.len() != 36 {
        return None;
    }
    let b = text.as_bytes();
    if b[8] != b'-' || b[13] != b'-' || b[18] != b'-' || b[23] != b'-' {
        return None;
    }
    let compact: String = text.chars().filter(|c| *c != '-').collect();
    unhex::<16>(&compact)
}

/// Enforces the product limits. Control characters other than TAB, LF and CR are
/// rejected: that keeps the worst-case JSON expansion at 4 bytes per character, so
/// the 65,536-character leg limit holds for EVERY accepted body, not just typical ones.
pub fn validate_body(body: &str) -> Result<()> {
    if body.is_empty() {
        return Err(Error::BodyInvalid);
    }
    if body.len() > MAX_BODY_BYTES || body.chars().count() > MAX_BODY_CHARS {
        return Err(Error::BodyTooLarge);
    }
    if body
        .chars()
        .any(|c| c.is_control() && c.is_ascii() && !matches!(c, '\t' | '\n' | '\r'))
    {
        return Err(Error::BodyInvalid);
    }
    Ok(())
}

/// Serialize an envelope (the Olm plaintext). Caller zeroizes via the returned wrapper.
pub fn encode(envelope: &Envelope) -> Result<Zeroizing<Vec<u8>>> {
    validate_body(&envelope.body)?;
    let wire = Wire {
        v: ENVELOPE_VERSION,
        conversation_id: uuid_to_text(&envelope.conversation_id),
        client_message_id: uuid_to_text(&envelope.client_message_id),
        sender: Party {
            user_id: uuid_to_text(&envelope.sender_user_id),
            device_id: uuid_to_text(&envelope.sender_device_id),
        },
        recipient: Party {
            user_id: uuid_to_text(&envelope.recipient_user_id),
            device_id: uuid_to_text(&envelope.recipient_device_id),
        },
        kind: KIND_TEXT.to_owned(),
        body: envelope.body.clone(),
    };
    serde_json::to_vec(&wire)
        .map(Zeroizing::new)
        .map_err(|_| Error::Internal)
}

/// Strictly parse a decrypted plaintext. Any deviation is `InvalidEnvelope`.
pub fn decode(plaintext: &[u8]) -> Result<Envelope> {
    if plaintext.is_empty() || plaintext.len() > MAX_ENVELOPE_BYTES {
        return Err(Error::InvalidEnvelope);
    }
    let wire: Wire = serde_json::from_slice(plaintext).map_err(|_| Error::InvalidEnvelope)?;
    if wire.v != ENVELOPE_VERSION || wire.kind != KIND_TEXT {
        return Err(Error::InvalidEnvelope);
    }
    let id = |s: &str| uuid_from_text(s).ok_or(Error::InvalidEnvelope);
    validate_body(&wire.body).map_err(|_| Error::InvalidEnvelope)?;
    Ok(Envelope {
        conversation_id: id(&wire.conversation_id)?,
        client_message_id: id(&wire.client_message_id)?,
        sender_user_id: id(&wire.sender.user_id)?,
        sender_device_id: id(&wire.sender.device_id)?,
        recipient_user_id: id(&wire.recipient.user_id)?,
        recipient_device_id: id(&wire.recipient.device_id)?,
        body: wire.body,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Envelope {
        Envelope {
            conversation_id: [1; 16],
            client_message_id: [2; 16],
            sender_user_id: [3; 16],
            sender_device_id: [4; 16],
            recipient_user_id: [5; 16],
            recipient_device_id: [6; 16],
            body: "hello".into(),
        }
    }

    #[test]
    fn uuid_text_is_canonical_and_roundtrips() {
        let id: [u8; 16] = *b"\x12\x34\x56\x78\x9a\xbc\xde\xf0\x01\x23\x45\x67\x89\xab\xcd\xef";
        let text = uuid_to_text(&id);
        assert_eq!(text, "12345678-9abc-def0-0123-456789abcdef");
        assert_eq!(uuid_from_text(&text), Some(id));
        assert_eq!(uuid_from_text(&text.to_uppercase()), None);
        assert_eq!(uuid_from_text("12345678-9abc-def0-0123-456789abcde"), None);
        assert_eq!(uuid_from_text("123456789abcdef0012345678-9abcdef"), None);
        assert_eq!(uuid_from_text("12345678-9abc-def0-0123-456789abcdeg"), None);
    }

    #[test]
    fn roundtrip_and_exact_bytes() {
        let bytes = encode(&sample()).unwrap();
        let text = std::str::from_utf8(&bytes).unwrap();
        assert!(text.starts_with("{\"v\":1,\"conversation_id\":\"01010101-"));
        assert!(text.ends_with("\"kind\":\"text\",\"body\":\"hello\"}"));
        let parsed = decode(&bytes).unwrap();
        assert_eq!(parsed.body, "hello");
        assert_eq!(parsed.sender_device_id, [4; 16]);
    }

    #[test]
    fn rejects_malformed_envelopes() {
        let good = String::from_utf8(encode(&sample()).unwrap().to_vec()).unwrap();
        for bad in [
            good.replace("\"v\":1", "\"v\":2"),
            good.replace("\"kind\":\"text\"", "\"kind\":\"image\""),
            good.replace("\"kind\"", "\"extra\":1,\"kind\""),
            good.replace("01010101-", "01010101_"),
            good.replace(
                "\"conversation_id\"",
                "\"conversation_id\":\"x\",\"conversation_id\"",
            ),
            format!("{good}x"),
            String::new(),
        ] {
            assert_eq!(decode(bad.as_bytes()).err(), Some(Error::InvalidEnvelope));
        }
    }

    #[test]
    fn body_limits() {
        assert_eq!(validate_body(""), Err(Error::BodyInvalid));
        assert!(validate_body(&"a".repeat(MAX_BODY_CHARS)).is_ok());
        assert_eq!(
            validate_body(&"a".repeat(MAX_BODY_CHARS + 1)),
            Err(Error::BodyTooLarge)
        );
        // 10,000 four-byte characters is exactly the byte limit.
        assert!(validate_body(&"\u{1F600}".repeat(MAX_BODY_CHARS)).is_ok());
        assert_eq!(
            validate_body(&"\u{1F600}".repeat(MAX_BODY_CHARS + 1)),
            Err(Error::BodyTooLarge)
        );
        assert_eq!(validate_body("a\u{0}b"), Err(Error::BodyInvalid));
        assert_eq!(validate_body("a\u{1b}b"), Err(Error::BodyInvalid));
        assert!(validate_body("line1\nline2\r\n\tindented").is_ok());
    }
}

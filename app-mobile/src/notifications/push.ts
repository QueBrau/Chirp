/** Push notifications: FCM registration, content-free handling, deep-link routing (SPEC §5).
 *
 * Notifications are CONTENT-FREE by design (SPEC §8.1): title like "New message
 * from Maria", never message bodies — the server couldn't include them anyway
 * (ciphertext only). Registration + listeners land in milestone 4; the pure
 * deep-link mapping below is real and testable now.
 */

/** Data payload of a Chirp push — ids only, never content. */
export interface ContentFreePushData {
  conversation_id?: string;
  chapter_id?: string;
}

/**
 * Request permission, fetch the Expo push token, and register it with the backend.
 * Returns the token, or null when unavailable (permission denied / simulator).
 * TODO(milestone-4): expo-notifications permission + token + backend registration.
 */
export async function registerForPush(): Promise<string | null> {
  // Explicit no-op until milestone 4 (CONVENTIONS: stubs no-op explicitly).
  return null;
}

/**
 * Attach foreground/background notification listeners that route taps via
 * deepLinkForPush. Returns a cleanup function that detaches them.
 * TODO(milestone-4): wire expo-notifications response listeners + expo-router navigation.
 */
export function attachNotificationHandlers(): () => void {
  // Explicit no-op until milestone 4.
  return () => {};
}

/** Map a content-free push payload to the chirp:// deep link it should open. */
export function deepLinkForPush(data: ContentFreePushData): string | null {
  if (data.conversation_id) return `chirp://messages/${data.conversation_id}`;
  if (data.chapter_id) return `chirp://chapter`;
  return null;
}

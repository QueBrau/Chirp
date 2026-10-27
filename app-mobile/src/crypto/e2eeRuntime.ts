/** The real engine: the native core, the v2 API and the phone's storage wired together
 * (board c451). The logic is in `e2eeEngine.ts`; this file only picks the adapters.
 *
 * Importing it is safe everywhere: on web, Android, Expo Go, or a build cut before the
 * native module existed, `isE2EEAvailable()` is false and the engine reports `unavailable`
 * without touching storage or the network.
 */

import { Platform } from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as ExpoCrypto from "expo-crypto";
import * as SecureStore from "expo-secure-store";

import * as api from "../api/e2ee";
import { getConversation, leaveConversation } from "../api/messages";
import { currentIdentity, ownsIdentity } from "../auth/identity";
import { E2eeEngine, type ConversationsPort } from "./e2eeEngine";
import type { KvPort } from "./e2eeStore";
import { chirpCrypto, isE2EEAvailable } from "./native";

/** Keychain, readable after the first unlock like the native store key, never synced to another device. */
const secureKv: KvPort = {
  get: (key) => SecureStore.getItemAsync(key, { keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY }),
  set: (key, value) =>
    SecureStore.setItemAsync(key, value, { keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY }),
  remove: (key) => SecureStore.deleteItemAsync(key),
};

const plainKv: KvPort = {
  get: (key) => AsyncStorage.getItem(key),
  set: (key, value) => AsyncStorage.setItem(key, value),
  remove: (key) => AsyncStorage.removeItem(key),
};

const conversations: ConversationsPort = {
  leave: (conversationId) => leaveConversation(conversationId),
  async getPeer(conversationId, myUserId) {
    const conversation = await getConversation(conversationId);
    if (conversation.kind !== "dm") return { kind: "group", peerUserId: null };
    const peer = (conversation.members ?? []).find((member) => member.left_at === null && member.user_id !== myUserId);
    return { kind: "dm", peerUserId: peer?.user_id ?? null };
  },
};

function create(): E2eeEngine {
  return new E2eeEngine({
    captureOwner: () => { const owner = currentIdentity(); return () => ownsIdentity(owner); },
    crypto: chirpCrypto,
    api,
    secure: secureKv,
    plain: plainKv,
    available: isE2EEAvailable,
    canHaveNativeHistory: () => Platform.OS === "ios",
    newId: () => ExpoCrypto.randomUUID().toLowerCase(),
    now: () => Date.now(),
    conversations,
    // Development builds only, and only a place name plus a fixed code: never a body, an id or an error message.
    report: (where, code) => {
      if (__DEV__) console.warn(`[e2ee] ${where}: ${code}`);
    },
  });
}

// One engine for the app. Kept on globalThis so a Fast Refresh of this file in development
// re-adopts the running engine instead of making a second one that finds the store open.
const SLOT = "__chirpE2eeEngine";
const holder = globalThis as unknown as Record<string, E2eeEngine | undefined>;
export const e2ee: E2eeEngine = (holder[SLOT] ??= create());

/** Secure storage and real APIs for the account-deletion cleanup coordinator. */

import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";
import { checkDeletionCompletion, createDeletionCompletionToken } from "../api/dataRequests";
import { currentIdentity, ownsIdentity, type AuthIdentity } from "../auth/identity";
import { signOutUser } from "../auth/session";
import { DeletionCompletion } from "./deletionCompletion";
import { e2ee } from "./e2eeRuntime";

const completion = new DeletionCompletion({
  secure: {
    get: key => SecureStore.getItemAsync(key),
    set: (key, value) => SecureStore.setItemAsync(key, value, { keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY }),
    remove: key => SecureStore.deleteItemAsync(key),
  },
  issue: createDeletionCompletionToken,
  check: checkDeletionCompletion,
  purge: userId => e2ee.purgeAccount(userId),
  currentIdentity, ownsIdentity, now: () => Date.now(),
  afterPurge: async (record, owner) => {
    if (ownsIdentity(owner) && owner.uid === record.firebaseUid) await signOutUser();
  },
});

/** Keep proof even on an iOS build whose native module cannot currently open an older store. */
export async function rememberDeletion(userId: string, requestId: string, owner: AuthIdentity): Promise<boolean> {
  if (Platform.OS !== "ios") return false;
  await completion.remember(userId, requestId, owner);
  return true;
}

export async function checkPendingDeletions(): Promise<void> {
  if (Platform.OS === "ios") await completion.checkPending();
}

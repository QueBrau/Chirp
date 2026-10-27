/** Continue checking saved completion proofs after Firebase has removed the account. */

import { useEffect } from "react";
import { AppState } from "react-native";
import { onIdentityChanged } from "../auth/identity";
import { checkPendingDeletions } from "./deletionCompletionRuntime";

export function AccountDeletionMonitor() {
  useEffect(() => {
    const check = () => {
      if (AppState.currentState !== "background" && AppState.currentState !== "inactive") {
        void checkPendingDeletions().catch(() => undefined);
      }
    };
    check();
    const offIdentity = onIdentityChanged(check);
    const state = AppState.addEventListener("change", next => { if (next === "active") check(); });
    const timer = setInterval(check, 60_000);
    return () => { offIdentity(); state.remove(); clearInterval(timer); };
  }, []);
  return null;
}

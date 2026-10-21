/** Binds the encrypted-DM engine to the signed-in session (board c451, design section 7).
 *
 * One provider under SessionProvider:
 *  - session ready (real Firebase user or the emulated dev uid) -> engine.start(user id);
 *  - sign-out, account switch, suspension -> engine.stop() (locks the store, wipes nothing);
 *  - a live "message" hint for a v2 conversation -> sync that conversation;
 *  - the socket (re)opening, or the app returning to the foreground -> recheck the device
 *    and replay the outbox;
 *  - (c463) a ready phone asks which new phones are waiting for its approval: on becoming
 *    ready, on the foreground, on a socket reconnect, and every half minute while in use; a
 *    phone that is itself waiting for approval re-checks every few seconds while its banner is
 *    up, bounded, so it becomes ready without a restart.
 *
 * `useE2ee()` is the state the screens branch on; `useEncryptedDm()` is one DM thread.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AppState } from "react-native";

import { useSession } from "@/auth";
import { chirpSocket, isMessageEvent } from "@/realtime/socket";
import type { PlaintextMessage } from "./native";
import { buildThread, type ThreadItem } from "./e2eeThread";
import { e2ee } from "./e2eeRuntime";
import { startBoundedPoll } from "./e2eeTrust";
import {
  APPROVAL_POLL_BUDGET,
  APPROVAL_POLL_MS,
  PENDING_POLL_MS,
  type DmTrust,
  type E2eeState,
  type PendingDevice,
  type SendOutcome,
} from "./e2eeTypes";
import type { ThreadSnapshot } from "./e2eeEngine";

const E2eeContext = createContext<E2eeState | null>(null);

const swallow = () => undefined;

export function E2eeProvider({ children }: { children: ReactNode }) {
  const { status, user } = useSession();
  const userId = user?.id ?? null;
  // Recoverable = the account is fine and only the network is not: keep the device up.
  const active = userId !== null && (status === "ready" || status === "recoverable");
  const [state, setState] = useState<E2eeState>(() => e2ee.getState());

  useEffect(
    () =>
      e2ee.subscribe((event) => {
        if (event.type === "state") setState(e2ee.getState());
      }),
    [],
  );

  useEffect(() => {
    if (!active || userId === null) return;
    void e2ee.start(userId);
    return () => {
      void e2ee.stop();
    };
  }, [active, userId]);

  // c463, an approved phone: look for new phones waiting for approval.
  const ready = active && state.status === "ready";
  const [foreground, setForeground] = useState(() => AppState.currentState !== "background");
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (next) => setForeground(next === "active"));
    return () => subscription.remove();
  }, []);
  useEffect(() => {
    if (!ready || !foreground) return;
    // Returning to the foreground also lifts any "decide later" the person chose earlier.
    void e2ee.refreshPendingDevices({ foreground: true }).catch(swallow);
    const timer = setInterval(() => void e2ee.refreshPendingDevices().catch(swallow), PENDING_POLL_MS);
    return () => clearInterval(timer);
  }, [ready, foreground]);

  // c463, a phone that is itself waiting for approval: ask often, but never forever.
  const waiting = active && state.status === "pending_approval";
  useEffect(() => {
    if (!waiting || !foreground) return;
    const poll = startBoundedPoll({
      intervalMs: APPROVAL_POLL_MS,
      budget: APPROVAL_POLL_BUDGET,
      tick: async () => (await e2ee.recheck("manual")).status !== "pending_approval",
    });
    return () => poll.stop();
  }, [waiting, foreground]);

  useEffect(() => {
    if (!active) return;
    const offEvent = chirpSocket.onEvent((event) => {
      // Only v2 messages carry an envelope version; legacy ciphertext events are not ours.
      if (isMessageEvent(event) && event.envelope_version !== undefined) {
        void e2ee.syncFromHint(event.conversation_id).catch(swallow);
      }
    });
    // The source matters to one state only: a server that has no v2 API yet is looked at again
    // when the app returns to the foreground (once per session), never on a socket reconnect.
    const wake = (source: "foreground" | "reconnect") => {
      void e2ee
        .recheck(source)
        .then(() => e2ee.replayOutbox())
        .then(() => e2ee.refreshPendingDevices({ foreground: source === "foreground" }))
        .catch(swallow);
    };
    const offStatus = chirpSocket.onStatus((next) => {
      if (next === "open") wake("reconnect");
    });
    const appState = AppState.addEventListener("change", (next) => {
      if (next === "active") wake("foreground");
    });
    return () => {
      offEvent();
      offStatus();
      appState.remove();
    };
  }, [active]);

  return <E2eeContext.Provider value={state}>{children}</E2eeContext.Provider>;
}

/** Where this phone's device stands. Outside the provider (a test harness) it is inactive. */
export function useE2ee(): E2eeState {
  return useContext(E2eeContext) ?? e2ee.getState();
}

// ---------------------------------------------------------------------------
// One DM thread
// ---------------------------------------------------------------------------

export interface EncryptedDm {
  items: ThreadItem[];
  /** True until the first read of the local store has finished. */
  loading: boolean;
  hasOlder: boolean;
  loadOlder: () => Promise<void>;
  /** Hard-stop status of the conversation; `block` null = clear to send. */
  trust: DmTrust;
  /**
   * c463: the person accepted the changed code that is stopping sends. Re-pins exactly what
   * the banner described (refusing if it moved again) and re-reads the trust picture.
   * Rejects with an `E2eeTrustError` the screen can word.
   */
  acceptNewCode: () => Promise<void>;
  /** Sync with the server and re-read the local store. */
  refresh: () => Promise<void>;
  send: (body: string) => Promise<SendOutcome>;
  retry: (clientMessageId: string) => Promise<void>;
}

export function useEncryptedDm(conversationId: string, peerUserId: string | null, myUserId: string | null): EncryptedDm {
  const state = useE2ee();
  const ready = state.status === "ready";
  const [newest, setNewest] = useState<ThreadSnapshot | null>(null);
  const [older, setOlder] = useState<PlaintextMessage[]>([]);
  const [olderHasMore, setOlderHasMore] = useState<boolean | null>(null);
  const [trust, setTrust] = useState<DmTrust>(NO_BLOCK);
  const generation = useRef(0);

  const reload = useCallback(async () => {
    if (!ready) return;
    const mine = ++generation.current;
    const snapshot = await e2ee.loadThread(conversationId);
    if (generation.current === mine) setNewest(snapshot);
  }, [ready, conversationId]);

  const refresh = useCallback(async () => {
    if (!ready) return;
    await reload();
    if (peerUserId !== null) {
      try {
        await e2ee.syncConversation(conversationId, peerUserId);
      } catch {
        // Offline or a transient fault: the local store still has everything received so far.
      }
      setTrust(await e2ee.trustFor(peerUserId));
    }
    await reload();
  }, [ready, reload, conversationId, peerUserId]);

  useEffect(() => {
    setNewest(null);
    setOlder([]);
    setOlderHasMore(null);
    setTrust(NO_BLOCK);
  }, [conversationId]);

  useEffect(() => {
    if (!ready) return;
    void refresh().catch(swallow);
    const off = e2ee.subscribe((event) => {
      if (event.type === "thread" && event.conversationId === conversationId) {
        void reload().catch(swallow);
        // A message from a phone this phone had not seen refreshes the directories while it is
        // read, so the trust picture may have moved: show a hard stop the moment it is known.
        if (peerUserId !== null) void e2ee.trustFor(peerUserId).then(setTrust).catch(swallow);
      }
      // A verified mark or an accepted code changed: re-read the trust picture.
      if (event.type === "trust" && peerUserId !== null) void e2ee.trustFor(peerUserId).then(setTrust).catch(swallow);
    });
    const offStatus = chirpSocket.onStatus((next) => {
      if (next === "open") void refresh().catch(swallow);
    });
    return () => {
      off();
      offStatus();
    };
  }, [ready, conversationId, peerUserId, refresh, reload]);

  const loadOlder = useCallback(async () => {
    if (!ready || newest === null) return;
    const all = [...newest.messages, ...older];
    if (all.length === 0) return;
    const before = Math.min(...all.map((message) => message.seq));
    const page = await e2ee.loadThread(conversationId, before);
    setOlder((current) => [...current, ...page.messages]);
    setOlderHasMore(page.hasOlder);
  }, [ready, newest, older, conversationId]);

  const send = useCallback(
    async (body: string) => {
      if (peerUserId === null) throw new Error("no_peer");
      try {
        return await e2ee.sendDm({ conversationId, peerUserId, body });
      } finally {
        void reload().catch(swallow);
        void e2ee.trustFor(peerUserId).then(setTrust).catch(swallow);
      }
    },
    [conversationId, peerUserId, reload],
  );

  const acceptNewCode = useCallback(async () => {
    const block = trust.block;
    if (block === null) return;
    try {
      await e2ee.acceptIdentityChange(block.userId, trust.fingerprint);
    } finally {
      // Whatever happened, show what is true now: the banner clears, changes to the next
      // problem (the other side of the conversation), or stays if the code moved again.
      if (peerUserId !== null) setTrust(await e2ee.trustFor(peerUserId));
    }
  }, [trust, peerUserId]);

  const retry = useCallback(
    async (clientMessageId: string) => {
      try {
        await e2ee.retrySend(clientMessageId);
      } catch {
        // The entry records why it failed; the thread shows it.
      }
      await reload();
    },
    [reload],
  );

  const items = useMemo<ThreadItem[]>(() => {
    if (newest === null || myUserId === null) return [];
    const seen = new Set<number>();
    const merged: PlaintextMessage[] = [];
    for (const message of [...newest.messages, ...older]) {
      if (seen.has(message.seq)) continue;
      seen.add(message.seq);
      merged.push(message);
    }
    return buildThread({
      messages: merged,
      hasOlder: olderHasMore ?? newest.hasOlder,
      outbox: newest.outbox,
      undecryptable: newest.undecryptable,
      myUserId,
    });
  }, [newest, older, olderHasMore, myUserId]);

  return {
    items,
    loading: ready && newest === null,
    hasOlder: olderHasMore ?? newest?.hasOlder ?? false,
    loadOlder,
    trust,
    acceptNewCode,
    refresh,
    send,
    retry,
  };
}

const NO_BLOCK: DmTrust = Object.freeze({ block: null, verified: false, fingerprint: null });

// ---------------------------------------------------------------------------
// c463: trust, approvals and the pending phone's own code
// ---------------------------------------------------------------------------

/**
 * The trust picture of one DM, kept current for a header: re-read on mount, when this phone
 * becomes ready, and whenever a trust decision changes. `null` peer or a phone that is not
 * ready reads as "nothing claimed".
 */
export function useDmTrust(peerUserId: string | null): DmTrust {
  const { status } = useE2ee();
  const [trust, setTrust] = useState<DmTrust>(NO_BLOCK);
  useEffect(() => {
    if (status !== "ready" || peerUserId === null) {
      setTrust(NO_BLOCK);
      return;
    }
    let live = true;
    const read = () => void e2ee.trustFor(peerUserId).then((next) => live && setTrust(next)).catch(swallow);
    read();
    const off = e2ee.subscribe((event) => {
      // A trust decision, or a message that may have refreshed the directories while it was read.
      if (event.type === "trust" || event.type === "thread") read();
    });
    return () => {
      live = false;
      off();
    };
  }, [status, peerUserId]);
  return trust;
}

/** New phones of this account waiting for this phone's approval (empty unless this phone is ready). */
export function usePendingApprovals(): PendingDevice[] {
  const [pending, setPending] = useState<PendingDevice[]>(() => e2ee.getPendingApprovals());
  useEffect(() => {
    setPending(e2ee.getPendingApprovals());
    return e2ee.subscribe((event) => {
      if (event.type === "approvals") setPending(e2ee.getPendingApprovals());
    });
  }, []);
  return pending;
}

/** This phone's own comparison code while it waits for approval, else null. */
export function useOwnComparisonCode(): string | null {
  const { status, deviceId } = useE2ee();
  const [code, setCode] = useState<string | null>(null);
  useEffect(() => {
    if (status !== "pending_approval") {
      setCode(null);
      return;
    }
    let live = true;
    void e2ee.ownComparisonCode().then((next) => live && setCode(next)).catch(swallow);
    return () => {
      live = false;
    };
  }, [status, deviceId]);
  return code;
}

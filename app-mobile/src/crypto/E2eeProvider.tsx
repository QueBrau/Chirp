/** Binds the encrypted-DM engine to the signed-in session (board c451, design section 7).
 *
 * One provider under SessionProvider:
 *  - session ready (real Firebase user or the emulated dev uid) -> engine.start(user id);
 *  - sign-out, account switch, suspension -> engine.stop() (locks the store, wipes nothing);
 *  - a live "message" hint for a v2 conversation -> sync that conversation;
 *  - the socket (re)opening, or the app returning to the foreground -> recheck the device
 *    and replay the outbox.
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
import type { DmTrust, E2eeState, SendOutcome } from "./e2eeTypes";
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

  useEffect(() => {
    if (!active) return;
    const offEvent = chirpSocket.onEvent((event) => {
      // Only v2 messages carry an envelope version; legacy ciphertext events are not ours.
      if (isMessageEvent(event) && event.envelope_version !== undefined) {
        void e2ee.syncFromHint(event.conversation_id).catch(swallow);
      }
    });
    const wake = () => {
      void e2ee
        .recheck()
        .then(() => e2ee.replayOutbox())
        .catch(swallow);
    };
    const offStatus = chirpSocket.onStatus((next) => {
      if (next === "open") wake();
    });
    const appState = AppState.addEventListener("change", (next) => {
      if (next === "active") wake();
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
  const [trust, setTrust] = useState<DmTrust>({ block: null });
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
    setTrust({ block: null });
  }, [conversationId]);

  useEffect(() => {
    if (!ready) return;
    void refresh().catch(swallow);
    const off = e2ee.subscribe((event) => {
      if (event.type === "thread" && event.conversationId === conversationId) void reload().catch(swallow);
    });
    const offStatus = chirpSocket.onStatus((next) => {
      if (next === "open") void refresh().catch(swallow);
    });
    return () => {
      off();
      offStatus();
    };
  }, [ready, conversationId, refresh, reload]);

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
    refresh,
    send,
    retry,
  };
}

/** WebSocket client for the /ws gateway: typed events, auth, reconnect with backoff.
 *
 * Event frames mirror the backend pubsub contract (app.ws.pubsub):
 * `{"type": "<event_type>", ...payload}`. Events never carry ciphertext beyond
 * the opaque base64 `ciphertext` blob field (CONVENTIONS).
 *
 * c129 found this unwired entirely (grepped app/ and src/, zero hits on
 * `.connect()`) and fixed the 4403 handling without wiring a consumer, since
 * that was a separate, much larger feature than a suspension screen. c63 is
 * that feature: SessionProvider now calls `.connect()` once a session is
 * really authenticated (see its `status === "ready"` effect) and
 * `.disconnect()` otherwise — suspended included, since the gateway would
 * 4403 a suspended caller's connection attempt anyway (c126).
 */

import { wsAuthProtocol, wsUrl } from "../api/client";
import type { MessageType } from "../api/messages";

/** New message fan-out, published by the messages router on POST. */
export interface MessageSocketEvent {
  type: "message";
  conversation_id: string;
  message_id: string;
  sender_device_id?: string;
  message_type?: MessageType;
  /** Opaque base64 blob — the only ciphertext field ever allowed in events. */
  ciphertext?: string;
  // c63: routers/messages.py's publish_to_user call includes this (the real
  // message's created_at, ISO-formatted) — it was in every event this gateway
  // has ever sent, just never declared here since nothing read it yet.
  created_at?: string;
}

/** Forward-compatible catch-all for event types added after this file was written. */
export interface UnknownSocketEvent {
  type: string;
  [key: string]: unknown;
}

export type SocketEvent = MessageSocketEvent | UnknownSocketEvent;

export function isMessageEvent(event: SocketEvent): event is MessageSocketEvent {
  return event.type === "message";
}

export type SocketStatus = "idle" | "connecting" | "open" | "closed" | "suspended";

// Mirrors ws/gateway.py's WS_ACCOUNT_SUSPENDED (board c126/c129). No shared
// constants file crosses the backend/mobile boundary in this repo, so this is
// duplicated rather than imported — kept in sync by the comment on both sides
// pointing at the same board card.
const WS_CLOSE_ACCOUNT_SUSPENDED = 4403;

export type SocketEventListener = (event: SocketEvent) => void;
export type SocketStatusListener = (status: SocketStatus) => void;

const BASE_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;
// c152: how long a connection has to stay open before it counts as a real
// recovery for backoff-reset purposes, not just a handshake that happened to
// complete before the server tore it down. Picked with margin on both sides:
// the observed failure (auth+accept succeed, then pubsub.subscribe() fails
// with 4503) closes in roughly 100-200ms, so 5s is nowhere near that; and 5s
// is short enough that a connection which genuinely recovers isn't stuck
// throttled at a stale attempt count for long afterward.
const STABLE_CONNECTION_MS = 5_000;

/** Single WS connection to the gateway; stream is server → client only. */
export class ChirpSocket {
  private ws: WebSocket | null = null;
  private status: SocketStatus = "idle";
  private shouldRun = false;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  // c152: armed in onopen, fires STABLE_CONNECTION_MS later and is what
  // actually resets reconnectAttempts — NOT onopen itself. Cleared on close or
  // disconnect so a stale timer from a connection that already died can never
  // reset the counter a later, still-failing attempt is relying on.
  private stabilityTimer: ReturnType<typeof setTimeout> | null = null;
  private eventListeners = new Set<SocketEventListener>();
  private statusListeners = new Set<SocketStatusListener>();

  getStatus(): SocketStatus {
    return this.status;
  }

  /** Subscribe to decoded events; returns an unsubscribe function. */
  onEvent(listener: SocketEventListener): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  /** Subscribe to connection status changes; returns an unsubscribe function. */
  onStatus(listener: SocketStatusListener): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  /** Open the connection (token rides the subprotocol — see wsAuthProtocol()). */
  connect(): void {
    this.shouldRun = true;
    this.open();
  }

  /** Close and stop reconnecting. */
  disconnect(): void {
    this.shouldRun = false;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.stabilityTimer !== null) {
      clearTimeout(this.stabilityTimer);
      this.stabilityTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.setStatus("closed");
  }

  private open(): void {
    if (!this.shouldRun || this.ws) return;
    this.setStatus("connecting");
    // security-pass item 7: token goes in as a subprotocol, not the URL. No
    // token yet (never signed in, or SessionProvider raced ahead of
    // setAuthToken) means an anonymous handshake the server will 4401 — same
    // failure as before, just no longer one that also wrote a bearer token
    // into Cloud Run's request-url logging on the way.
    const protocol = wsAuthProtocol();
    const ws = new WebSocket(wsUrl(), protocol !== null ? [protocol] : undefined);
    this.ws = ws;

    ws.onopen = () => {
      // c152: NOT an immediate reconnectAttempts reset — that was the bug.
      // ws.onopen fires as soon as the browser's handshake completes, and the
      // server's accept() genuinely succeeds before a downstream failure
      // (pubsub.subscribe() against a down Redis, 4503) tears the connection
      // back down ~100-200ms later. Resetting here meant every single one of
      // those cycles reset the counter right before scheduleReconnect() used
      // it, so the exponential math never actually grew: a client hammered a
      // permanently-failing gateway at a flat ~2s cadence, observed as 1,863
      // attempts in one hour against prod (board c152). The reset now only
      // fires if the connection is still open STABLE_CONNECTION_MS later —
      // proven via a throwaway harness porting this exact logic: the old
      // reset-on-open shape reproduces the flat cadence at thousands of
      // attempts/hour; requiring a survived duration instead drops it by
      // roughly 25x and lets the delay actually reach the 30s cap.
      this.stabilityTimer = setTimeout(() => {
        this.reconnectAttempts = 0;
        this.stabilityTimer = null;
      }, STABLE_CONNECTION_MS);
      this.setStatus("open");
    };

    ws.onmessage = (frame: { data: unknown }) => {
      if (typeof frame.data !== "string") return;
      let event: SocketEvent;
      try {
        event = JSON.parse(frame.data) as SocketEvent;
      } catch {
        return; // malformed frame — drop it
      }
      if (typeof event?.type !== "string") return;
      for (const listener of this.eventListeners) listener(event);
    };

    ws.onclose = (event: { code: number }) => {
      this.ws = null;
      // c152: this connection did not survive to reset the counter — cancel
      // the pending timer rather than let it fire later. Without this, a
      // stability timer armed by THIS attempt could still fire after a LATER
      // attempt has already started counting, wiping out backoff progress
      // that later attempt earned.
      if (this.stabilityTimer !== null) {
        clearTimeout(this.stabilityTimer);
        this.stabilityTimer = null;
      }
      // c129: a suspended account is never going to succeed on retry — the
      // condition that closed this connection doesn't clear on its own, only a
      // moderator's unsuspend does. Reconnecting into it is pointless traffic
      // and, if a future caller ever surfaces socket status directly, a
      // confusing "still connecting..." indicator over a state that already
      // has its own real screen (SessionProvider's "suspended" status, driven
      // by the same MeOut.suspended_at this mirrors). No consumer reads this
      // status today (see the module docstring), so this is currently a no-op
      // in practice — fixed at the class level so it's correct the moment one
      // does, rather than left for whoever wires this up to rediscover.
      if (event.code === WS_CLOSE_ACCOUNT_SUSPENDED) {
        this.setStatus("suspended");
        return;
      }
      this.setStatus("closed");
      this.scheduleReconnect();
    };

    ws.onerror = () => {
      // onclose fires next and drives the reconnect; nothing to do here.
    };
  }

  /** Exponential backoff with jitter: 1s, 2s, 4s ... capped at 30s. */
  private scheduleReconnect(): void {
    if (!this.shouldRun || this.reconnectTimer !== null) return;
    const exponential = BASE_RECONNECT_DELAY_MS * 2 ** this.reconnectAttempts;
    const delay = Math.min(exponential, MAX_RECONNECT_DELAY_MS) * (0.5 + Math.random() * 0.5);
    this.reconnectAttempts += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private setStatus(status: SocketStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const listener of this.statusListeners) listener(status);
  }
}

/** App-wide singleton — one socket per app session. */
export const chirpSocket = new ChirpSocket();

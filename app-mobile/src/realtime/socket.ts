/** WebSocket client for the /ws gateway: typed events, auth, reconnect with backoff.
 *
 * Event frames mirror the backend pubsub contract (app.ws.pubsub):
 * `{"type": "<event_type>", ...payload}`. Events never carry ciphertext beyond
 * the opaque base64 `ciphertext` blob field (CONVENTIONS).
 *
 * FOUND WHILE WIRING c129: nothing in app-mobile imports this module. The
 * `chirpSocket` singleton at the bottom exists but `.connect()` is never
 * called anywhere — grepped app/ and src/, zero hits. So the realtime gateway
 * this client talks to (board c21, tested and live server-side) is not
 * actually turned on in the shipped app. That is a separate, much larger
 * feature (activating real-time messaging client-side) than a suspension
 * screen, and is not built here. The 4403 handling below is correct
 * regardless — a class should behave right whether or not it is used yet —
 * but is currently inert in practice until something calls `.connect()`.
 */

import { wsUrl } from "../api/client";
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

/** Single WS connection to the gateway; stream is server → client only. */
export class ChirpSocket {
  private ws: WebSocket | null = null;
  private status: SocketStatus = "idle";
  private shouldRun = false;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
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

  /** Open the connection (token rides the ?token= query param — see wsUrl()). */
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
    this.ws?.close();
    this.ws = null;
    this.setStatus("closed");
  }

  private open(): void {
    if (!this.shouldRun || this.ws) return;
    this.setStatus("connecting");
    const ws = new WebSocket(wsUrl());
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectAttempts = 0;
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

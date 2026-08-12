/** WebSocket client for the /ws gateway: typed events, auth, reconnect with backoff.
 *
 * Event frames mirror the backend pubsub contract (app.ws.pubsub):
 * `{"type": "<event_type>", ...payload}`. Events never carry ciphertext beyond
 * the opaque base64 `ciphertext` blob field (CONVENTIONS).
 * In mock mode (USE_MOCKS) connect() is an explicit no-op.
 */

import { USE_MOCKS, wsUrl } from "../api/client";
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

export type SocketStatus = "idle" | "connecting" | "open" | "closed";

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
    if (USE_MOCKS) {
      // Mock mode: no backend, no socket. Explicit no-op (CONVENTIONS: stubs no-op explicitly).
      return;
    }
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

    ws.onclose = () => {
      this.ws = null;
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

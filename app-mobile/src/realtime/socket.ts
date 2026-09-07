/** WebSocket client for the /ws gateway: typed events, auth, reconnect with backoff.
 *
 * Event frames mirror the backend pubsub contract (app.ws.pubsub):
 * `{"type": "<event_type>", ...payload}`. Events never carry ciphertext beyond
 * the opaque base64 `ciphertext` blob field (CONVENTIONS).
 *
 * SessionProvider starts one run for a ready account. c346 bounds transport
 * retries and delegates terminal auth revalidation back to that same Provider;
 * a transport outage never decides the Firebase account has signed out.
 */

import { wsAuthProtocol, wsUrl } from "../api/client";
import { Operation } from "../api/operation";
import { currentIdentity, ownsIdentity, onIdentityChanged, type AuthIdentity } from "../auth/identity";
import type { MessageType } from "../api/messages";
import type { PollOut } from "../api/polls";

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

/**
 * Poll fan-out, published by the polls router on open/vote/close/delete (c162).
 *
 * `poll` carries the AGGREGATE only and deliberately has no `my_option_id`: one
 * message goes to every member of the chapter, and that field means something
 * different for each of them. A client merging this event must keep whatever
 * `my_option_id` it already had, which is always correct because only your own
 * vote can change it.
 *
 * Absent on "deleted" — there is no poll left to describe.
 */
export interface PollSocketEvent {
  type: "poll";
  action: "opened" | "updated" | "deleted";
  chapter_id: string;
  poll_id: string;
  poll?: Omit<PollOut, "my_option_id">;
}

/** Forward-compatible catch-all for event types added after this file was written. */
export interface UnknownSocketEvent {
  type: string;
  [key: string]: unknown;
}

export type SocketEvent = MessageSocketEvent | PollSocketEvent | UnknownSocketEvent;

export function isMessageEvent(event: SocketEvent): event is MessageSocketEvent {
  return event.type === "message";
}

export function isPollEvent(event: SocketEvent): event is PollSocketEvent {
  return event.type === "poll";
}

export type SocketStatus = "idle" | "connecting" | "open" | "closed" | "suspended" | "revalidating" | "paused";
export type SocketEventListener = (event: SocketEvent) => void;
export type SocketStatusListener = (status: SocketStatus) => void;

/** SessionProvider owns the one auth decision; the socket never signs Firebase out. */
export interface SocketAuthHandlers {
  revalidate: (owner: AuthIdentity, signal: AbortSignal) => Promise<boolean>;
  exhausted: (owner: AuthIdentity) => void;
}

const BASE_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;
const MAX_RECONNECT_ATTEMPTS = 6;
const CONNECT_TIMEOUT_MS = 10_000;
const AUTH_REVALIDATION_TIMEOUT_MS = 10_000;
// c152: brief open/4503 cycles do not reset backoff. Only a stable connection does.
const STABLE_CONNECTION_MS = 5_000;
const WS_AUTH_FAILED = 4401;
const WS_ACCOUNT_SUSPENDED = 4403;

/** One owned connection, one bounded retry run, and at most one auth recovery in it. */
export class ChirpSocket {
  private ws: WebSocket | null = null;
  private unsubscribeIdentity: (() => void) | null = null;
  private status: SocketStatus = "idle";
  private shouldRun = false;
  private paused = false;
  private run = 0;
  private runAbort: AbortController | null = null;
  private authOperation: Operation | null = null;
  private authAttempts = 0;
  private authHandlers: SocketAuthHandlers | null = null;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stabilityTimer: ReturnType<typeof setTimeout> | null = null;
  private connectTimer: ReturnType<typeof setTimeout> | null = null;
  private eventListeners = new Set<SocketEventListener>();
  private statusListeners = new Set<SocketStatusListener>();

  getStatus(): SocketStatus { return this.status; }

  onEvent(listener: SocketEventListener): () => void {
    this.eventListeners.add(listener);
    return () => { this.eventListeners.delete(listener); };
  }

  onStatus(listener: SocketStatusListener): () => void {
    this.statusListeners.add(listener);
    return () => { this.statusListeners.delete(listener); };
  }

  setAuthHandlers(handlers: SocketAuthHandlers): () => void {
    this.authHandlers = handlers;
    return () => { if (this.authHandlers === handlers) this.authHandlers = null; };
  }

  /** Repeated connect calls never replenish a live or paused run's retry budget. */
  connect(): void {
    if (this.shouldRun) return;
    this.shouldRun = true;
    this.paused = false;
    this.run += 1;
    this.runAbort = new AbortController();
    this.authAttempts = 0;
    this.reconnectAttempts = 0;
    this.unsubscribeIdentity = onIdentityChanged(() => this.disconnect());
    this.open();
  }

  /** Explicit user retry after Provider revalidates the current account. */
  retry(): void { this.disconnect(); this.connect(); }

  disconnect(): void {
    this.shouldRun = false;
    this.run += 1;
    this.unsubscribeIdentity?.();
    this.unsubscribeIdentity = null;
    this.runAbort?.abort();
    this.runAbort = null;
    this.authOperation?.cancel();
    this.authOperation = null;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.ws) this.retire(this.ws, true);
    this.setStatus("closed");
  }

  private ownsRun(run: number, owner: AuthIdentity): boolean {
    return this.shouldRun && this.run === run && ownsIdentity(owner);
  }

  private retire(ws: WebSocket, close: boolean): void {
    if (this.ws !== ws) return;
    this.ws = null;
    if (this.connectTimer !== null) clearTimeout(this.connectTimer);
    if (this.stabilityTimer !== null) clearTimeout(this.stabilityTimer);
    this.connectTimer = this.stabilityTimer = null;
    // Detach real handlers as well as guarding callbacks already queued by a runtime.
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    if (close) ws.close();
  }

  private pause(): void { this.paused = true; this.setStatus("paused"); }

  private open(): void {
    if (!this.shouldRun || this.paused || this.ws || this.authOperation) return;
    const owner = currentIdentity(), run = this.run;
    this.setStatus("connecting");
    if (!this.ownsRun(run, owner)) return;
    let ws: WebSocket;
    try {
      const protocol = wsAuthProtocol();
      ws = new WebSocket(wsUrl(), protocol !== null ? [protocol] : undefined);
    } catch {
      this.scheduleReconnect(run, owner);
      return;
    }
    this.ws = ws;
    const isCurrent = () => this.ws === ws && this.ownsRun(run, owner);
    const transientFailure = () => {
      if (!isCurrent()) return;
      this.retire(ws, true);
      this.setStatus("closed");
      this.scheduleReconnect(run, owner);
    };
    this.connectTimer = setTimeout(transientFailure, CONNECT_TIMEOUT_MS);
    ws.onopen = () => {
      if (!isCurrent()) return;
      if (this.connectTimer !== null) clearTimeout(this.connectTimer);
      this.connectTimer = null;
      this.stabilityTimer = setTimeout(() => {
        if (!isCurrent()) return;
        this.reconnectAttempts = 0;
        this.stabilityTimer = null;
      }, STABLE_CONNECTION_MS);
      this.setStatus("open");
    };
    ws.onmessage = (frame: { data: unknown }) => {
      if (!isCurrent() || typeof frame.data !== "string") return;
      let event: SocketEvent;
      try { event = JSON.parse(frame.data) as SocketEvent; } catch { return; }
      if (typeof event?.type !== "string") return;
      for (const listener of this.eventListeners) listener(event);
    };
    ws.onclose = (event: { code: number }) => {
      if (!isCurrent()) return;
      this.retire(ws, false);
      if (event.code === WS_AUTH_FAILED || event.code === WS_ACCOUNT_SUSPENDED) {
        void this.revalidate(run, owner);
      } else {
        this.setStatus("closed");
        this.scheduleReconnect(run, owner);
      }
    };
    ws.onerror = transientFailure;
  }

  private async revalidate(run: number, owner: AuthIdentity): Promise<void> {
    if (!this.ownsRun(run, owner)) return;
    const handlers = this.authHandlers;
    if (this.authAttempts >= 1 || !handlers) {
      this.pause();
      if (this.ownsRun(run, owner)) handlers?.exhausted(owner);
      return;
    }
    this.authAttempts += 1;
    const operation = new Operation({ timeoutMs: AUTH_REVALIDATION_TIMEOUT_MS, signal: this.runAbort?.signal }, owner);
    this.authOperation = operation;
    this.setStatus("revalidating");
    let ready = false;
    try { operation.assertCurrent(); ready = await operation.wait(handlers.revalidate(owner, operation.signal)); }
    catch { /* Provider owns the account decision and retry UI. */ }
    finally {
      operation.dispose();
      if (this.authOperation === operation) this.authOperation = null;
    }
    if (!this.ownsRun(run, owner)) return;
    if (ready && !operation.signal.aborted) this.open();
    else { this.pause(); if (this.ownsRun(run, owner)) handlers.exhausted(owner); }
  }

  /** Finite consecutive transport retries; reconnect success must survive five seconds. */
  private scheduleReconnect(run: number, owner: AuthIdentity): void {
    if (!this.ownsRun(run, owner) || this.paused || this.reconnectTimer !== null) return;
    if (this.reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) { this.pause(); return; }
    const delay = Math.min(BASE_RECONNECT_DELAY_MS * 2 ** this.reconnectAttempts, MAX_RECONNECT_DELAY_MS)
      * (0.5 + Math.random() * 0.5);
    this.reconnectAttempts += 1;
    const timer = setTimeout(() => {
      // A callback already queued when clearTimeout ran belongs to its old run.
      // It must not erase a replacement run's timer before checking ownership.
      if (!this.ownsRun(run, owner) || this.reconnectTimer !== timer) return;
      this.reconnectTimer = null;
      this.open();
    }, delay);
    this.reconnectTimer = timer;
  }

  private setStatus(status: SocketStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const listener of this.statusListeners) listener(status);
  }
}

export const chirpSocket = new ChirpSocket();

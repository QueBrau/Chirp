/** Shared vocabulary of the encrypted-DM wiring (board c451, E2EE-DESIGN.md section 7).
 *
 * Pure types and constants: no imports of Expo, React or the network, so the logic that
 * uses them can be executed by `npm run verify:e2ee-dm` with fakes for every boundary.
 */

import type { TrustState } from "./native";

// ---------------------------------------------------------------------------
// Device state
// ---------------------------------------------------------------------------

/**
 * Where this phone stands, as the UI sees it.
 *
 *  - `inactive`         no signed-in account, or the engine was stopped (sign-out).
 *  - `starting`         unlocking the store / registering / checking approval.
 *  - `ready`            registered, approved, and able to send and receive.
 *  - `pending_approval` registered, but another phone of this account must approve it
 *                       first. Also what a REINSTALLED phone ends up in: the server
 *                       still lists the old install's device as approved (M5 builds the
 *                       approve and Reset-security screens; M4 only surfaces it).
 *  - `unavailable`      this build has no native crypto module (web, Android, Expo Go,
 *                       a dev build cut before the module existed).
 *  - `error`            something failed; `reason` is a fixed code and a retry is safe.
 */
export type E2eeStatus =
  | "inactive"
  | "starting"
  | "ready"
  | "pending_approval"
  | "unavailable"
  | "error";

export interface E2eeState {
  status: E2eeStatus;
  userId: string | null;
  /** The server's id for THIS phone's device once it is registered. */
  deviceId: string | null;
  generation: number | null;
  /** A fixed code for `error` (never server or native text), else null. */
  reason: string | null;
}

export const INACTIVE_STATE: E2eeState = Object.freeze({
  status: "inactive",
  userId: null,
  deviceId: null,
  generation: null,
  reason: null,
});

// ---------------------------------------------------------------------------
// Constants (design section 7 step 2, section 5)
// ---------------------------------------------------------------------------

/** The only suite the server accepts (core/e2ee_v2.py SUITE_VODOZEMAC_OLM_V1). */
export const E2EE_SUITE = "vodozemac-olm-v1";
/** The only envelope version the server stores (schemas/e2ee_v2.py ENVELOPE_VERSION_1). */
export const E2EE_ENVELOPE_VERSION = 1;
/** One-time keys made at registration. */
export const INITIAL_ONE_TIME_KEYS = 100;
/** Below this many unconsumed server-side keys, top up. */
export const KEY_LOW_WATER = 50;
/** ...back up to this many. */
export const KEY_TARGET = 100;
/** The UI and the engine refuse a body longer than this (the core's MAX_BODY_CHARS). */
export const MAX_DM_BODY_CHARS = 10_000;
/** Rows per history request (server maximum is 200). */
export const HISTORY_PAGE = 50;
/** A sync gives up collecting after this many unseen rows; the next sync continues. */
export const MAX_SYNC_BACKLOG = 1_000;
/** Most placeholder rows remembered per conversation. */
export const MAX_REMEMBERED_FAILURES = 200;
/** Most unsent messages the outbox will hold (a runaway guard, not a product limit). */
export const MAX_OUTBOX = 100;
/** The label this phone's device is registered under. Generic on purpose. */
export const DEVICE_LABEL = "Chirp for iOS";

// ---------------------------------------------------------------------------
// Persisted app-side state
// ---------------------------------------------------------------------------

/** What the app remembers about this phone's device, per user. Not secret. */
export interface DeviceRecord {
  generation: number;
  /** Null until the server has answered the registration. */
  deviceId: string | null;
  /** True once the directory has listed this device as approved. An approved device
   * only stops being approved by being revoked, so this lets an offline start trust it. */
  approved: boolean;
}

export type OutboxStatus = "pending" | "failed";

/**
 * A message that has been (or is about to be) encrypted but is not yet acknowledged by the
 * server. It carries NO body: the plaintext copy lives in the encrypted native store
 * (`listMessages`), and a replay recovers the body from there by `clientMessageId`.
 */
export interface OutboxEntry {
  clientMessageId: string;
  conversationId: string;
  peerUserId: string;
  createdAtMs: number;
  attempts: number;
  status: OutboxStatus;
  /** Fixed code of the last permanent failure, when `status` is `failed`. */
  failure: string | null;
}

/** A message from the server that could not be decrypted: shown as a quiet placeholder. */
export interface UndecryptableRow {
  messageId: string;
  /** Server created_at, for ordering against other placeholders. */
  createdAt: string;
  senderDeviceId: string;
  /** Fixed code, never native or server text. */
  code: string;
  /** Highest local `seq` in the conversation when this was recorded: the placeholder is
   * drawn right after that message. */
  anchorSeq: number;
}

export interface AppState {
  outbox: OutboxEntry[];
  undecryptable: Record<string, UndecryptableRow[]>;
}

// ---------------------------------------------------------------------------
// Send results and errors
// ---------------------------------------------------------------------------

/** A trust hard stop: sending to (or with) this user's devices is blocked. */
export interface TrustBlock {
  userId: string;
  reason: Exclude<TrustState, "trusted">;
}

export type SendFailureCode =
  /** This phone is not set up (state is not `ready`). */
  | "not_ready"
  /** Nothing to send, or longer than MAX_DM_BODY_CHARS. */
  | "invalid_body"
  /** A trust hard stop; see `block`. */
  | "blocked"
  /** The other person has no phone that can receive encrypted messages yet. */
  | "peer_no_devices"
  /** The server will not let this conversation exchange keys (blocked, left, stranger). */
  | "not_allowed"
  /** This phone's device was revoked, or is not approved. */
  | "device_unusable"
  /** Nothing could be encrypted because the network was needed first (directory, key
   * claim) and is down. The text was not stored; the composer keeps it. */
  | "offline"
  /** The server or the core refused for a reason retrying will not fix. */
  | "rejected";

export class E2eeSendError extends Error {
  readonly code: SendFailureCode;
  readonly block: TrustBlock | null;

  constructor(code: SendFailureCode, block: TrustBlock | null = null) {
    super(code);
    this.name = "E2eeSendError";
    this.code = code;
    this.block = block;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/**
 * `sent`    the server has the message.
 * `queued`  the message is encrypted and committed locally but the network (or a server
 *           hiccup) stopped delivery; it stays in the outbox and goes out on the next
 *           replay (app start, reconnect, foreground, or the user's retry).
 */
export type SendOutcome = { kind: "sent"; clientMessageId: string } | { kind: "queued"; clientMessageId: string };

/** The trust picture for one conversation, for the banner. `block` null = clear to send. */
export interface DmTrust {
  block: TrustBlock | null;
}

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

/**
 * `E2eeState.reason` of an `unavailable` phone whose SERVER has no v2 API yet (a 404/405 from
 * POST /v2/devices or GET /v2/users/{id}/devices). The screens say "coming soon", not an
 * error; nothing is registered or wiped; it is looked at again on the next app start and at
 * most once on a foreground within a session.
 */
export const SERVER_NOT_READY = "server_not_ready";

/**
 * `E2eeState.reason` of an `unavailable` phone whose ACCOUNT is being deleted (the backend
 * answers every v2 write with 423 `account_deletion_in_progress` while a deletion runs).
 * Not an error and not retryable: nothing here will change until the account is gone, so
 * there is no retry button and no re-check; the screens say so in plain words.
 */
export const ACCOUNT_DELETING = "account_deletion_in_progress";
/**
 * `E2eeState.reason` of an `error` phone whose device the server says is revoked (c463): the
 * account's other phones removed it (Reset security) or its owner refused it ("Not me"). It is
 * final: there is nothing to retry, and a fresh install is how a phone comes back.
 */
export const DEVICE_REVOKED = "device_revoked";
/** `error` reason: this install has sent too many registrations the server never confirmed. */
export const REGISTRATION_UNCONFIRMED = "registration_unconfirmed";
/**
 * The most registration requests one install may send without ever being told a device id.
 * A registration whose answer is lost can leave an unseen PENDING device on the server, and
 * the next start cannot tell, so it tries again; this bounds that to a few attempts instead
 * of an unbounded run of orphan devices (the server caps them at 5 active, 20 retained).
 */
export const MAX_UNCONFIRMED_REGISTRATIONS = 3;

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
  /** Registration requests sent since the last confirmed device id (absent = 0). */
  attempts?: number;
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
  /** An account in this conversation is being deleted: the server refuses every write (423). */
  | "account_deletion"
  /** The server answered but is busy or struggling (429, 5xx). Nothing was encrypted. */
  | "server_busy"
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

/**
 * The trust picture for one conversation, for the banner. `block` null = clear to send.
 * `verified` (c463) is true only when there is no block AND the person marked the CURRENT
 * safety number as verified: any change to either side's phones makes the marker stale.
 * `fingerprint` identifies the directory of the user the block is about, as seen when the
 * block was computed (null when there is none): accepting passes it back so the accept
 * refuses if that directory moved again in between.
 */
export interface DmTrust {
  block: TrustBlock | null;
  verified: boolean;
  fingerprint: string | null;
}

// ---------------------------------------------------------------------------
// Trust UX (board c463, E2EE milestone M5)
// ---------------------------------------------------------------------------

/**
 * A pending device of THIS account, as the approving phone shows it: the public binding it
 * will sign (exactly what the server listed) and the short comparison code derived from the
 * new device's Ed25519 identity. The new phone shows the same code for itself.
 */
export interface PendingDevice {
  deviceId: string;
  generation: number;
  label: string | null;
  createdAt: string;
  identityCurve25519: string;
  identityEd25519: string;
  bindingSignature: string;
  /** Digits only (`deviceComparisonCode`); `groupComparisonCode` spaces it for display. */
  code: string;
}

/** What the safety-number screen draws, from fresh directories (never from a cache). */
export interface SafetyView {
  peerUserId: string;
  /** 60 digits, no separators: identical on both phones. */
  number: string;
  /** The contact's standing against what this phone pinned. */
  state: TrustState;
  /** This account's own standing (its own phones can be the reason for a hard stop). */
  ownState: TrustState;
  /** The current number is the one marked verified (and nothing is pending acceptance). */
  verified: boolean;
  /**
   * Identifies the exact directories this view was computed from. `setVerified` refuses when
   * they have moved since the person looked, so a person never verifies something other than
   * what was on screen. `acceptIdentityChange` does the same with the one directory it is
   * about (`peerFingerprint` for the contact, `ownFingerprint` for this account's own phones).
   */
  fingerprint: string;
  peerFingerprint: string;
  ownFingerprint: string;
}

export type TrustFailureCode =
  /** This phone is not set up (state is not `ready`). */
  | "not_ready"
  /** Reset security is only for a phone that is waiting for approval. */
  | "not_pending"
  /** The contact has no phone that can receive messages yet, so there is nothing to compare. */
  | "peer_no_devices"
  /** The directories changed after the person looked: look again before deciding. */
  | "changed_again"
  /** A hard stop is active, so the safety number cannot be marked verified yet. */
  | "blocked"
  /** The pending phone is already gone (approved elsewhere, revoked, or never there). */
  | "gone"
  /** No answer from the server. Nothing was changed that a retry cannot finish. */
  | "network"
  /** An account deletion is running: the server refuses every v2 write. */
  | "account_deletion"
  /** Reset security could not leave every other phone revoked, or did not end as the root. */
  | "reset_incomplete"
  /** The server or the core refused for a reason retrying will not fix. */
  | "rejected";

export class E2eeTrustError extends Error {
  readonly code: TrustFailureCode;

  constructor(code: TrustFailureCode) {
    super(code);
    this.name = "E2eeTrustError";
    this.code = code;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** The most passes Reset security makes over "revoke every other phone" before it gives up. */
export const MAX_RESET_REVOKE_PASSES = 2;
/** How often a pending phone asks whether it was approved, while its banner is on screen. The directory read it uses is rate limited (60 per 10 minutes), so this stays under it. */
export const APPROVAL_POLL_MS = 12_000;
/** ...and how many times at most per wait (12 s x 50 = 10 minutes), so it cannot poll forever. */
export const APPROVAL_POLL_BUDGET = 50;
/** How often an approved phone, while in use, asks whether a new phone is waiting. */
export const PENDING_POLL_MS = 30_000;

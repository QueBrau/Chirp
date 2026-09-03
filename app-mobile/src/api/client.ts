/** Typed fetch wrapper for the Chirp backend: base URL, auth header injection. */

import { getIdToken, hasFirebaseConfig } from "@/auth";

/** The live Cloud Run API, used whenever EXPO_PUBLIC_API_URL is not set. */
const DEFAULT_API_BASE_URL = "https://chirp-api-593616178468.us-central1.run.app";

/**
 * The realtime service that PAIRS with DEFAULT_API_BASE_URL (board c272).
 *
 * Not decoration and not a duplicate of eas.json: this is what an unconfigured
 * build must use, and scripts/verify-ws-url.mjs asserts it stays byte-identical
 * to the value eas.json ships for preview/production, so the two cannot drift
 * apart silently.
 */
const DEFAULT_WS_URL = "wss://chirp-ws-593616178468.us-central1.run.app/ws";

/**
 * Backend origin. Defaults to the live Cloud Run backend; override with
 * EXPO_PUBLIC_API_URL (e.g. "http://localhost:8000") to point at a local
 * FastAPI dev server instead.
 */
export const API_BASE_URL: string =
  process.env.EXPO_PUBLIC_API_URL ?? DEFAULT_API_BASE_URL;

let authToken: string | null = null;
let debugFirebaseUid: string | null = null;

/** Store the Firebase ID token sent as `Authorization: Bearer <token>` on every request. */
export function setAuthToken(token: string | null): void {
  authToken = token;
}

/** Emulated-auth mode only: uid sent as `X-Debug-Firebase-Uid` (backend auth_mode="emulated"). */
export function setDebugFirebaseUid(uid: string | null): void {
  debugFirebaseUid = uid;
}

/** Non-2xx response from the backend; `detail` mirrors FastAPI's `{"detail": ...}` body. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface RequestOptions {
  method?: HttpMethod;
  /** JSON-serialized body. */
  body?: unknown;
  /** Query params; undefined values are dropped. */
  query?: Record<string, string | number | boolean | undefined>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = `${API_BASE_URL}${path}`;
  if (!query) return url;
  const parts: string[] = [];
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined) continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length > 0 ? `${url}?${parts.join("&")}` : url;
}

/** Fire the actual network request with whatever bearer/debug headers are currently set. */
function doFetch(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
  if (debugFirebaseUid) headers["X-Debug-Firebase-Uid"] = debugFirebaseUid;

  return fetch(buildUrl(path, options.query), {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
}

/**
 * On a 401, retry ONCE after forcing a fresh Firebase ID token — the ~1hr token can
 * go stale between onIdTokenChanged refreshes (e.g. app resumed from background).
 * Gated on hasFirebaseConfig(): a no-op in mock/demo mode.
 */
async function fetchWithAuthRetry(path: string, options: RequestOptions): Promise<Response> {
  let response = await doFetch(path, options);

  if (response.status === 401 && hasFirebaseConfig()) {
    const freshToken = await getIdToken(true);
    if (freshToken) {
      setAuthToken(freshToken);
      response = await doFetch(path, options);
    }
  }

  return response;
}

/** Turn a fetch Response into the resolved payload, or throw ApiError on non-2xx. */
async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Perform an authenticated JSON request against the backend. Throws ApiError on non-2xx.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return parseResponse<T>(await fetchWithAuthRetry(path, options));
}

/**
 * Same auth/URL/error handling as `request`, but resolves the raw response body as
 * text instead of JSON-parsing it. For endpoints that don't return JSON — e.g. the
 * treasurer/secretary CSV exports (`/ledger/export.csv`, `/meetings/export.csv`).
 */
export async function requestText(path: string, options: RequestOptions = {}): Promise<string> {
  const response = await fetchWithAuthRetry(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  return response.text();
}

/**
 * Same auth/URL/error handling as `request`, but also returns the raw response
 * Headers alongside the parsed JSON body. For the rare endpoint that carries
 * metadata OUTSIDE the body — today just GET /chapters/{id}/posts' actives-only
 * "hidden content exists" signal (board c102) — rather than changing that
 * endpoint's long-established bare-array response shape, which several existing
 * backend tests and app-mobile's own listPosts() already parse directly.
 */
export async function requestWithHeaders<T>(
  path: string,
  options: RequestOptions = {},
): Promise<{ data: T; headers: Headers }> {
  const response = await fetchWithAuthRetry(path, options);
  const data = await parseResponse<T>(response);
  return { data, headers: response.headers };
}

/**
 * WebSocket URL for the gateway at `/ws` — no auth material on it (security-pass
 * item 7, ~Aug 22). It used to carry `?token=` because RN's WebSocket
 * constructor can't set arbitrary headers, but Cloud Run logs the full request
 * URL at the platform layer regardless of anything the app does in-process, so
 * a query-string token was never actually protectable. Pair with
 * wsAuthProtocol() for the second WebSocket() constructor argument instead —
 * RN CAN set that.
 *
 * c213: independently configurable via EXPO_PUBLIC_WS_URL, same read pattern
 * as API_BASE_URL above (only a truly unset var falls through — an explicit
 * empty string is honored, same as EXPO_PUBLIC_API_URL). When set, that value
 * is returned verbatim as the full endpoint (path included) — this is what lets
 * realtime move to its own Cloud Run service (chirp-ws) independently of
 * EXPO_PUBLIC_API_URL, with no client rebuild.
 *
 * c272: WHEN UNSET, THE HOST DECIDES, and scheme-swapping alone was wrong.
 * Deriving `wss://<api-host>/ws` is right for a LOCAL api — a developer pointing
 * EXPO_PUBLIC_API_URL at localhost must get a localhost socket, never prod
 * realtime — but it is wrong for the default, because the default api host is
 * chirp-api and realtime lives on chirp-ws. eas.json leaves both development
 * profiles unset on purpose (correct, and it stays that way), so a CLOUD dev
 * build sets no api url either, falls back to the prod api, and used to derive
 * wss://chirp-api/ws: prod api paired with a socket on the wrong service. That
 * is the c209/c213 split undone, and it is SILENT because both services run the
 * same image, so chirp-api answers /ws and messaging looks fine while chirp-ws
 * logs zero upgrades — the c246 shape exactly.
 *
 * So: explicit override wins; otherwise the DEFAULT api pairs with the DEFAULT
 * socket; otherwise (a custom/local host) derive from it as before. Each branch
 * is a case in scripts/verify-ws-url.mjs.
 */
export function wsUrl(): string {
  const configured = process.env.EXPO_PUBLIC_WS_URL;
  if (configured !== undefined) return configured;
  if (API_BASE_URL === DEFAULT_API_BASE_URL) return DEFAULT_WS_URL;
  return `${API_BASE_URL.replace(/^http/, "ws")}/ws`;
}

/**
 * The value to send as the WebSocket subprotocol — this IS the auth material
 * now (ws/gateway.py's `_offered_protocol`/`_resolve_uid`), not the URL. Same
 * precedence as every other request: a real token when signed in for real, the
 * debug uid in emulated mode.
 */
export function wsAuthProtocol(): string | null {
  return authToken ?? debugFirebaseUid;
}

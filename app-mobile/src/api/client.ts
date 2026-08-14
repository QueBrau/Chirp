/** Typed fetch wrapper for the Chirp backend: base URL, auth header injection, USE_MOCKS switch. */

import { getIdToken, hasFirebaseConfig } from "@/auth";

/**
 * Backend origin. Defaults to the live Cloud Run backend; override with
 * EXPO_PUBLIC_API_URL (e.g. "http://localhost:8000") to point at a local
 * FastAPI dev server instead.
 */
export const API_BASE_URL: string =
  process.env.EXPO_PUBLIC_API_URL ?? "https://chirp-api-593616178468.us-central1.run.app";

/**
 * While true, every src/api function resolves from src/mocks/data.ts instead of
 * the network. Defaults to false (live backend); set EXPO_PUBLIC_USE_MOCKS=true
 * to bring back the mock/local-dev flow.
 */
export const USE_MOCKS: boolean = process.env.EXPO_PUBLIC_USE_MOCKS === "true";

let authToken: string | null = null;
let debugFirebaseUid: string | null = null;

/** Store the Firebase ID token sent as `Authorization: Bearer <token>` on every request. */
export function setAuthToken(token: string | null): void {
  authToken = token;
}

export function getAuthToken(): string | null {
  return authToken;
}

/** Emulated-auth mode only: uid sent as `X-Debug-Firebase-Uid` (backend auth_mode="emulated"). */
export function setDebugFirebaseUid(uid: string | null): void {
  debugFirebaseUid = uid;
}

export function getDebugFirebaseUid(): string | null {
  return debugFirebaseUid;
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
 * WebSocket URL for the gateway at `/ws`. RN WebSocket clients can't always set
 * headers, so the token rides the `?token=` query param (backend supports both;
 * in emulated mode the debug uid doubles as the token).
 */
export function wsUrl(): string {
  const base = API_BASE_URL.replace(/^http/, "ws");
  const token = authToken ?? debugFirebaseUid;
  return token ? `${base}/ws?token=${encodeURIComponent(token)}` : `${base}/ws`;
}

/** Resolve mock data as an API response (single place to add latency simulation later). */
export function mocked<T>(data: T): Promise<T> {
  return Promise.resolve(data);
}

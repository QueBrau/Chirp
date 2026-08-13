/** Typed fetch wrapper for the Chirp backend: base URL, auth header injection, USE_MOCKS switch. */

/** Backend origin. Point at the FastAPI dev server when flipping USE_MOCKS off. */
export const API_BASE_URL = "http://localhost:8000";

/**
 * While true, every src/api function resolves from src/mocks/data.ts instead of
 * the network. Wiring the real backend later = flip this to false.
 */
export const USE_MOCKS: boolean = true;

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

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
  if (debugFirebaseUid) headers["X-Debug-Firebase-Uid"] = debugFirebaseUid;
  return headers;
}

/**
 * Shared fetch + auth headers + ApiError handling for both `request` and
 * `requestText` below. Callers decide how to read the (already-ok) body.
 */
async function fetchOk(path: string, options: RequestOptions): Promise<Response> {
  const response = await fetch(buildUrl(path, options.query), {
    method: options.method ?? "GET",
    headers: authHeaders(),
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

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
  return response;
}

/** Perform an authenticated JSON request against the backend. Throws ApiError on non-2xx. */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await fetchOk(path, options);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Same auth/URL/error handling as `request`, but resolves the raw response body as
 * text instead of JSON-parsing it. For endpoints that don't return JSON — e.g. the
 * treasurer/secretary CSV exports (`/ledger/export.csv`, `/meetings/export.csv`).
 */
export async function requestText(path: string, options: RequestOptions = {}): Promise<string> {
  const response = await fetchOk(path, options);
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

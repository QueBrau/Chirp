/** Authenticated account export and deletion request API. */

import { request } from "./client";

export type DataRequestKind = "export" | "deletion";
export type DataRequestStatus =
  | "received"
  | "verifying"
  | "processing"
  | "ready"
  | "completed"
  | "partially_completed"
  | "blocked"
  | "failed"
  | "canceled";

/** Deliberately excludes credentials, tokens, and provider secrets. */
export interface DataRequestOut {
  id: string;
  kind: DataRequestKind;
  status: DataRequestStatus;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  download_url: string | null;
  expires_at: string | null;
  scope: string[];
  excluded: string[];
  retention_reasons: string[];
  failure_code: string | null;
}

/** Create one idempotent request for the signed-in account. */
export async function createDataRequest(kind: DataRequestKind): Promise<DataRequestOut> {
  return request<DataRequestOut>("/me/data-requests", {
    method: "POST",
    body: { kind },
  });
}

/** List the caller's requests; the server must scope this query to the auth subject. */
export async function listDataRequests(): Promise<DataRequestOut[]> {
  return request<DataRequestOut[]>("/me/data-requests");
}

export async function getDataRequest(requestId: string): Promise<DataRequestOut> {
  return request<DataRequestOut>(`/me/data-requests/${encodeURIComponent(requestId)}`);
}

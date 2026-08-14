/** Finance API: dues cycles, append-only ledger, spend approvals — routers/finance.py.
 *
 * There is intentionally NO update/delete for ledger entries (SPEC §8.2):
 * corrections are new entries with entry_type="correction" + corrects_entry_id.
 */

import { request, requestText } from "./client";

export type LedgerEntryType =
  | "dues_payment"
  | "expense"
  | "budget_allocation"
  | "correction"
  | "payout";

export type SpendApprovalStatus = "pending" | "approved" | "rejected";

export interface DuesCycleCreate {
  name: string;
  amount_cents: number;
  due_date: string; // ISO date (YYYY-MM-DD)
}

export interface DuesCycleOut {
  id: string;
  chapter_id: string;
  name: string;
  amount_cents: number;
  due_date: string;
  created_at: string;
}

/** created_by comes from auth server-side. */
export interface LedgerEntryCreate {
  entry_type: LedgerEntryType;
  amount_cents: number; // positive = in, negative = out
  category?: string | null;
  description?: string | null;
  related_user_id?: string | null;
  dues_cycle_id?: string | null;
  corrects_entry_id?: string | null;
}

export interface LedgerEntryOut {
  id: string;
  chapter_id: string;
  entry_type: LedgerEntryType;
  amount_cents: number;
  category: string | null;
  description: string | null;
  related_user_id: string | null;
  dues_cycle_id: string | null;
  stripe_payment_intent_id: string | null;
  corrects_entry_id: string | null;
  created_by: string;
  created_at: string;
}

export interface SpendApprovalCreate {
  amount_cents: number;
  description: string;
}

export interface SpendApprovalOut {
  id: string;
  chapter_id: string;
  requested_by: string;
  amount_cents: number;
  description: string;
  status: SpendApprovalStatus;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

export async function listDuesCycles(chapterId: string): Promise<DuesCycleOut[]> {
  return request<DuesCycleOut[]>(`/chapters/${chapterId}/dues-cycles`);
}

export async function createDuesCycle(
  chapterId: string,
  body: DuesCycleCreate,
): Promise<DuesCycleOut> {
  return request<DuesCycleOut>(`/chapters/${chapterId}/dues-cycles`, { method: "POST", body });
}

/** Treasurer ledger view with optional filters. */
export async function listLedger(
  chapterId: string,
  filters: { category?: string; from?: string; to?: string } = {},
): Promise<LedgerEntryOut[]> {
  return request<LedgerEntryOut[]>(`/chapters/${chapterId}/ledger`, { query: filters });
}

/**
 * Export the ledger as CSV — GET /chapters/{id}/ledger/export.csv. Same optional
 * filters as listLedger. Returns the raw CSV text; hand it to src/lib/export.ts's
 * shareCsv() to write + open the native share sheet.
 */
export async function exportLedgerCsv(
  chapterId: string,
  filters: { category?: string; from?: string; to?: string } = {},
): Promise<string> {
  return requestText(`/chapters/${chapterId}/ledger/export.csv`, { query: filters });
}

/** Append a ledger entry. Corrections: entry_type="correction" + corrects_entry_id. */
export async function createLedgerEntry(
  chapterId: string,
  body: LedgerEntryCreate,
): Promise<LedgerEntryOut> {
  return request<LedgerEntryOut>(`/chapters/${chapterId}/ledger`, { method: "POST", body });
}

export async function listSpendApprovals(chapterId: string): Promise<SpendApprovalOut[]> {
  return request<SpendApprovalOut[]>(`/chapters/${chapterId}/spend-approvals`);
}

export async function createSpendApproval(
  chapterId: string,
  body: SpendApprovalCreate,
): Promise<SpendApprovalOut> {
  return request<SpendApprovalOut>(`/chapters/${chapterId}/spend-approvals`, {
    method: "POST",
    body,
  });
}

/** Treasurer/president decision on a pending spend approval. */
export async function decideSpendApproval(
  chapterId: string,
  approvalId: string,
  status: "approved" | "rejected",
): Promise<SpendApprovalOut> {
  return request<SpendApprovalOut>(
    `/chapters/${chapterId}/spend-approvals/${approvalId}/decide`,
    { method: "POST", body: { status } },
  );
}

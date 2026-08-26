/** Finance API: dues cycles, append-only ledger, spend approvals — routers/finance.py.
 *
 * There is intentionally NO update/delete for ledger entries (SPEC §8.2):
 * corrections are new entries with entry_type="correction" + corrects_entry_id.
 */

import { ApiError, request, requestText } from "./client";

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

/** Status of a member's installment plan for one dues cycle (c197). */
export type DuesPaymentPlanStatus = "active" | "completed" | "canceled";

/** One scheduled slice of a DuesPaymentPlanOut — routers/finance.py's DuesPlanInstallmentOut. */
export interface DuesPlanInstallmentOut {
  id: string;
  plan_id: string;
  seq: number;
  amount_cents: number;
  due_date: string; // ISO date (YYYY-MM-DD) — a real scheduled date, honest to render
  paid_at: string | null; // real timestamp once a treasurer records the payment
  ledger_entry_id: string | null;
}

/** Response of GET /chapters/{chapterId}/dues-cycles/{cycleId}/plans/mine — routers/finance.py. */
export interface DuesPaymentPlanOut {
  id: string;
  chapter_id: string;
  dues_cycle_id: string;
  user_id: string;
  total_cents: number;
  installment_count: number;
  status: DuesPaymentPlanStatus;
  note: string | null;
  created_by: string;
  created_at: string;
  installments: DuesPlanInstallmentOut[];
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

/**
 * The signed-in member's own most-recent installment plan for one dues cycle
 * (any status), or null if they've never had one. The backend 404s
 * ("dues_payment_plan_not_found") rather than returning an empty body when no
 * plan exists — that 404 is the expected "no plan" case here, not a failure,
 * so it's swallowed into null; any other status still throws.
 */
export async function getMyPlan(
  chapterId: string,
  cycleId: string,
): Promise<DuesPaymentPlanOut | null> {
  try {
    return await request<DuesPaymentPlanOut>(
      `/chapters/${chapterId}/dues-cycles/${cycleId}/plans/mine`,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
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

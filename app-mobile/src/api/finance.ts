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

/**
 * One scheduled slice of a DuesPaymentPlanOut — routers/finance.py's DuesPlanInstallmentOut.
 *
 * paid_at and effective_paid answer DIFFERENT questions, and c235 exists because
 * this client was reading the first one for both. Per the backend schema's
 * docstring: paid_at is HISTORY (write-once, set the moment a treasurer records
 * the payment, never cleared even after the money moves back out), effective_paid
 * is STATUS (derived per request, nets any corrections against this installment's
 * ledger entry, and goes False once a refund takes the money back out).
 *
 * Rule for renderers: "is this still paid" reads effective_paid. paid_at is only
 * for showing WHEN it was originally recorded, and for gating the record-payment
 * action, which the backend claims on `paid_at IS NULL` (see dues-plans.tsx).
 */
export interface DuesPlanInstallmentOut {
  id: string;
  plan_id: string;
  seq: number;
  amount_cents: number;
  due_date: string; // ISO date (YYYY-MM-DD) — a real scheduled date, honest to render
  paid_at: string | null; // real timestamp once a treasurer records the payment
  ledger_entry_id: string | null;
  effective_paid: boolean; // c233/c235: current truth, False once refunded back out
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

/** One category's total SPENDING, positive cents. `category` is null when unset. */
export interface LedgerCategoryTotal {
  category: string | null;
  cents: number;
}

/** Running balance at the end of one calendar MONTH; `partial` marks the current one. */
export interface LedgerBalancePoint {
  period_start: string;
  balance_cents: number;
  partial: boolean;
}

/** Dues for the current cycle, netted server-side through the house definition. */
export interface LedgerDuesSummary {
  cycle_id: string;
  amount_cents: number;
  collected_cents: number;
  paid_members: number;
  /** The other half of the same three-way split, reported so the treasurer and
   * president surfaces are directly comparable - a cross-endpoint test asserts they
   * agree, which is what stops the two drifting apart again. */
  on_plan_members: number;
}

/**
 * Every number the treasurer dashboard renders, computed server-side (c258).
 *
 * The screen used to derive these by reducing the ledger LIST. That is only safe while
 * the list is complete, so it had to stop before the list could be paginated - a total
 * over page one, rendered as the total, is the worst kind of wrong on a money screen.
 */
export interface LedgerSummaryOut {
  balance_cents: number;
  entry_count: number;
  categories: LedgerCategoryTotal[];
  trend: LedgerBalancePoint[];
  dues: LedgerDuesSummary | null;
}

export async function getLedgerSummary(
  chapterId: string,
  filters: { category?: string; from?: string; to?: string } = {},
): Promise<LedgerSummaryOut> {
  return request<LedgerSummaryOut>(`/chapters/${chapterId}/ledger/summary`, {
    query: filters,
  });
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

// ---- dues payment plans (board card c195/c196) ----
//
// A member pays a dues cycle in full OR through a plan, never both — the backend
// enforces this (409 already_paid / on_payment_plan / payment_in_progress) and this
// client surfaces those as honest errors rather than pre-guessing every case.
// Kept in this file, not chapters.ts, so c197's parallel work on the member/roster
// client stays a clean rebase against this addition.

// DuesPaymentPlanStatus / DuesPlanInstallmentOut / DuesPaymentPlanOut are defined
// once above (c197's read-side addition); the create-body types below are c196's.

/** One scheduled slice in the body of POST .../dues-cycles/{cycleId}/plans. */
export interface DuesPlanInstallmentCreate {
  amount_cents: number; // > 0
  due_date: string; // ISO date (YYYY-MM-DD)
}

/**
 * Body for POST /chapters/{chapterId}/dues-cycles/{cycleId}/plans. total_cents is
 * NOT sent — the server always uses the cycle's own amount_cents and 422s
 * (installments_must_sum_to_cycle_amount) unless `installments` sums to exactly
 * that; installment_count must equal installments.length (422
 * installment_count_mismatch otherwise).
 */
export interface DuesPaymentPlanCreate {
  user_id: string;
  installment_count: number; // > 0
  note?: string | null;
  installments: DuesPlanInstallmentCreate[];
}

/** Every plan against this cycle, any status, newest first; treasurer/president only. */
export async function listDuesPaymentPlans(
  chapterId: string,
  cycleId: string,
): Promise<DuesPaymentPlanOut[]> {
  return request<DuesPaymentPlanOut[]>(`/chapters/${chapterId}/dues-cycles/${cycleId}/plans`);
}

/**
 * Set up an installment plan for one member's dues cycle; treasurer/president only.
 * 422 installments_must_sum_to_cycle_amount / installment_count_mismatch.
 * 409 already_paid / on_payment_plan / payment_in_progress.
 */
export async function createDuesPaymentPlan(
  chapterId: string,
  cycleId: string,
  body: DuesPaymentPlanCreate,
): Promise<DuesPaymentPlanOut> {
  return request<DuesPaymentPlanOut>(`/chapters/${chapterId}/dues-cycles/${cycleId}/plans`, {
    method: "POST",
    body,
  });
}

/**
 * Record one installment as paid; treasurer/president only. Appends a
 * dues_installment ledger row and completes the plan once every installment is
 * paid. 409 installment_already_paid / plan_not_active.
 */
export async function recordDuesInstallmentPayment(
  chapterId: string,
  planId: string,
  seq: number,
  note?: string | null,
): Promise<DuesPlanInstallmentOut> {
  return request<DuesPlanInstallmentOut>(
    `/chapters/${chapterId}/dues-plans/${planId}/installments/${seq}/record-payment`,
    { method: "POST", body: { note: note ?? null } },
  );
}

/** Cancel an active plan; treasurer/president only. 409 plan_not_active. */
export async function cancelDuesPaymentPlan(
  chapterId: string,
  planId: string,
): Promise<DuesPaymentPlanOut> {
  return request<DuesPaymentPlanOut>(`/chapters/${chapterId}/dues-plans/${planId}/cancel`, {
    method: "POST",
  });
}

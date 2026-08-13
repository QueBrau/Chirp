/** Finance API: dues cycles, append-only ledger, spend approvals — routers/finance.py.
 *
 * There is intentionally NO update/delete for ledger entries (SPEC §8.2):
 * corrections are new entries with entry_type="correction" + corrects_entry_id.
 */

import { mocked, request, requestText, USE_MOCKS } from "./client";
import {
  MOCK_CURRENT_USER,
  MOCK_DUES_CYCLES,
  MOCK_LEDGER_ENTRIES,
  MOCK_SPEND_APPROVALS,
  newMockId,
  nowIso,
} from "../mocks/data";

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
  if (USE_MOCKS) return mocked(MOCK_DUES_CYCLES.filter((c) => c.chapter_id === chapterId));
  return request<DuesCycleOut[]>(`/chapters/${chapterId}/dues-cycles`);
}

export async function createDuesCycle(
  chapterId: string,
  body: DuesCycleCreate,
): Promise<DuesCycleOut> {
  if (USE_MOCKS) {
    const cycle: DuesCycleOut = {
      id: newMockId("cycle"),
      chapter_id: chapterId,
      name: body.name,
      amount_cents: body.amount_cents,
      due_date: body.due_date,
      created_at: nowIso(),
    };
    MOCK_DUES_CYCLES.push(cycle);
    return mocked(cycle);
  }
  return request<DuesCycleOut>(`/chapters/${chapterId}/dues-cycles`, { method: "POST", body });
}

/** Treasurer ledger view with optional filters. */
export async function listLedger(
  chapterId: string,
  filters: { category?: string; from?: string; to?: string } = {},
): Promise<LedgerEntryOut[]> {
  if (USE_MOCKS) {
    return mocked(
      MOCK_LEDGER_ENTRIES.filter(
        (e) =>
          e.chapter_id === chapterId &&
          (filters.category === undefined || e.category === filters.category),
      ),
    );
  }
  return request<LedgerEntryOut[]>(`/chapters/${chapterId}/ledger`, { query: filters });
}

/** Quote a CSV field if it contains a comma, quote, or newline; escape embedded quotes. */
function csvField(value: string | number | null): string {
  if (value === null) return "";
  const str = String(value);
  return /[",\n]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
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
  if (USE_MOCKS) {
    const rows = MOCK_LEDGER_ENTRIES.filter(
      (e) =>
        e.chapter_id === chapterId &&
        (filters.category === undefined || e.category === filters.category),
    );
    const header = "id,entry_type,amount_cents,category,description,created_at";
    const lines = rows.map((e) =>
      [
        csvField(e.id),
        csvField(e.entry_type),
        csvField(e.amount_cents),
        csvField(e.category),
        csvField(e.description),
        csvField(e.created_at),
      ].join(","),
    );
    return mocked([header, ...lines].join("\n") + "\n");
  }
  return requestText(`/chapters/${chapterId}/ledger/export.csv`, { query: filters });
}

/** Append a ledger entry. Corrections: entry_type="correction" + corrects_entry_id. */
export async function createLedgerEntry(
  chapterId: string,
  body: LedgerEntryCreate,
): Promise<LedgerEntryOut> {
  if (USE_MOCKS) {
    const entry: LedgerEntryOut = {
      id: newMockId("led"),
      chapter_id: chapterId,
      entry_type: body.entry_type,
      amount_cents: body.amount_cents,
      category: body.category ?? null,
      description: body.description ?? null,
      related_user_id: body.related_user_id ?? null,
      dues_cycle_id: body.dues_cycle_id ?? null,
      stripe_payment_intent_id: null,
      corrects_entry_id: body.corrects_entry_id ?? null,
      created_by: MOCK_CURRENT_USER.id,
      created_at: nowIso(),
    };
    MOCK_LEDGER_ENTRIES.push(entry);
    return mocked(entry);
  }
  return request<LedgerEntryOut>(`/chapters/${chapterId}/ledger`, { method: "POST", body });
}

export async function listSpendApprovals(chapterId: string): Promise<SpendApprovalOut[]> {
  if (USE_MOCKS) return mocked(MOCK_SPEND_APPROVALS.filter((s) => s.chapter_id === chapterId));
  return request<SpendApprovalOut[]>(`/chapters/${chapterId}/spend-approvals`);
}

export async function createSpendApproval(
  chapterId: string,
  body: SpendApprovalCreate,
): Promise<SpendApprovalOut> {
  if (USE_MOCKS) {
    const approval: SpendApprovalOut = {
      id: newMockId("spend"),
      chapter_id: chapterId,
      requested_by: MOCK_CURRENT_USER.id,
      amount_cents: body.amount_cents,
      description: body.description,
      status: "pending",
      decided_by: null,
      decided_at: null,
      created_at: nowIso(),
    };
    MOCK_SPEND_APPROVALS.push(approval);
    return mocked(approval);
  }
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
  if (USE_MOCKS) {
    const approval = MOCK_SPEND_APPROVALS.find((s) => s.id === approvalId);
    if (approval) {
      approval.status = status;
      approval.decided_by = MOCK_CURRENT_USER.id;
      approval.decided_at = nowIso();
      return mocked(approval);
    }
    return mocked(MOCK_SPEND_APPROVALS[0]);
  }
  return request<SpendApprovalOut>(
    `/chapters/${chapterId}/spend-approvals/${approvalId}/decide`,
    { method: "POST", body: { status } },
  );
}

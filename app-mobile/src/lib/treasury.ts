/**
 * Pure derivations from the ledger for the treasurer dashboard's charts.
 *
 * Kept free of React and of react-native so the numbers behind the pictures can be
 * checked directly. A chart that is drawn perfectly from a wrong total is worse
 * than no chart: it is confidently wrong about somebody's money.
 *
 * Everything here works in CENTS and never rounds. Formatting to dollars happens
 * once, at the edge, in the screen.
 */

import type { DuesCycleOut, LedgerEntryOut } from "@/api/finance";

/** One point on the running-balance series. `x` is epoch ms so gaps stay to scale. */
export interface BalancePoint {
  x: number;
  y: number;
}

/**
 * Cumulative balance after each entry, oldest first.
 *
 * The ledger is append-only (CONVENTIONS "Finance rules"), so the balance at any
 * moment is just the running sum — there is no edit history to reconcile, and a
 * correction is an ordinary later entry that moves the line like any other.
 *
 * Entries are re-sorted here rather than trusted: the screen holds them newest-first
 * for the list, and summing in that order draws the chart backwards.
 */
export function runningBalance(entries: LedgerEntryOut[]): BalancePoint[] {
  const chronological = [...entries].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const points: BalancePoint[] = [];
  let total = 0;
  for (const entry of chronological) {
    total += entry.amount_cents;
    const at = Date.parse(entry.created_at);
    // A row with an unparseable timestamp would poison every x with NaN and blank
    // the whole chart; it still belongs in the running total, so carry the value
    // forward on the previous instant instead of dropping the money.
    const x = Number.isNaN(at) ? (points[points.length - 1]?.x ?? 0) : at;
    points.push({ x, y: total });
  }
  return points;
}

export interface CategorySlice {
  label: string;
  cents: number;
  /** True for the folded remainder, which wears the neutral rather than a hue. */
  isOther: boolean;
}

/** Categories beyond this fold into "Other" — a donut stops being readable past ~6. */
export const MAX_CATEGORY_SLICES = 5;

const UNCATEGORISED = "Uncategorised";

/**
 * Money OUT grouped by category, largest first, with the tail folded into "Other".
 *
 * Only negative entries are counted, and they are returned as POSITIVE magnitudes:
 * a part-to-whole chart of spending answers "what did we spend it on", so mixing
 * income in would make the slices shares of a total nobody has.
 *
 * The fold is not cosmetic. Past roughly six segments adjacent slices stop being
 * separable — so the tail is summed into one honest bucket rather than drawn as
 * slivers in colours the palette was never validated for.
 */
export function spendByCategory(
  entries: LedgerEntryOut[],
  maxSlices: number = MAX_CATEGORY_SLICES,
): CategorySlice[] {
  const totals = new Map<string, number>();
  for (const entry of entries) {
    if (entry.amount_cents >= 0) continue;
    const label = entry.category?.trim() ? entry.category.trim() : UNCATEGORISED;
    totals.set(label, (totals.get(label) ?? 0) + Math.abs(entry.amount_cents));
  }

  const ranked = [...totals.entries()]
    .map(([label, cents]) => ({ label, cents, isOther: false }))
    // Ties broken by label so the order — and therefore every slice's colour — is
    // stable between renders instead of depending on Map insertion order.
    .sort((a, b) => b.cents - a.cents || a.label.localeCompare(b.label));

  if (ranked.length <= maxSlices) return ranked;

  const head = ranked.slice(0, maxSlices);
  const tail = ranked.slice(maxSlices);
  const otherCents = tail.reduce((sum, slice) => sum + slice.cents, 0);
  return [...head, { label: `Other (${tail.length})`, cents: otherCents, isOther: true }];
}

export interface DuesProgress {
  collectedCents: number;
  expectedCents: number;
  paidCount: number;
  memberCount: number;
  /** 0..1, clamped — a cycle can be over-collected and a meter cannot exceed full. */
  fraction: number;
  /** True when more came in than was billed, which the caption must say out loud. */
  overCollected: boolean;
}

/**
 * Progress on one dues cycle: what was collected against what the cycle bills.
 *
 * Expected is `amount per member x members`, which is the only target the API can
 * express today — there is no per-member assessment table. When the roster has not
 * loaded (`memberCount` 0) expected is 0 and the caller shows counts, not a ratio,
 * rather than drawing a meter against a denominator it does not have.
 */
export function duesProgress(
  cycle: DuesCycleOut | undefined,
  entries: LedgerEntryOut[],
  memberCount: number,
): DuesProgress | null {
  if (cycle === undefined) return null;

  const payments = entries.filter(
    (entry) => entry.entry_type === "dues_payment" && entry.dues_cycle_id === cycle.id,
  );
  const collectedCents = payments.reduce((sum, entry) => sum + Math.abs(entry.amount_cents), 0);
  const expectedCents = cycle.amount_cents * memberCount;

  return {
    collectedCents,
    expectedCents,
    paidCount: payments.length,
    memberCount,
    fraction: expectedCents > 0 ? Math.min(collectedCents / expectedCents, 1) : 0,
    overCollected: expectedCents > 0 && collectedCents > expectedCents,
  };
}

/** Totals for the in/out summary beside the trend. Both returned positive. */
export function inOutTotals(entries: LedgerEntryOut[]): { inCents: number; outCents: number } {
  let inCents = 0;
  let outCents = 0;
  for (const entry of entries) {
    if (entry.amount_cents >= 0) inCents += entry.amount_cents;
    else outCents += Math.abs(entry.amount_cents);
  }
  return { inCents, outCents };
}

/**
 * Stable colour-slot per category label.
 *
 * `spendByCategory` returns slices largest-first because that is the useful reading
 * order, but colour must follow the ENTITY and never its rank: if "formal" overtakes
 * "rush" between two loads, a rank-indexed palette repaints both, and the reader sees
 * two categories apparently swap identity when nothing about them changed.
 *
 * So the display order and the colour assignment are decoupled. Each label has a
 * preferred slot derived from its own characters and nothing else; collisions walk
 * forward to the next free slot, in a fixed alphabetical pass so the result depends
 * only on the SET of labels, never on the order they arrived in.
 */
export function assignCategorySlots(labels: string[], slotCount: number): Map<string, number> {
  const assignment = new Map<string, number>();
  if (slotCount <= 0) return assignment;

  const taken = new Set<number>();
  // Alphabetical, not input order: two renders of the same categories must resolve
  // collisions identically regardless of which happened to be biggest that day.
  for (const label of [...labels].sort((a, b) => a.localeCompare(b))) {
    if (assignment.has(label)) continue;
    let hash = 0;
    for (let i = 0; i < label.length; i += 1) {
      // djb2-ish; `| 0` keeps it in int32 so the value cannot drift with length.
      hash = (hash * 33 + label.charCodeAt(i)) | 0;
    }
    let slot = Math.abs(hash) % slotCount;
    let steps = 0;
    while (taken.has(slot) && steps < slotCount) {
      slot = (slot + 1) % slotCount;
      steps += 1;
    }
    taken.add(slot);
    assignment.set(label, slot);
  }
  return assignment;
}

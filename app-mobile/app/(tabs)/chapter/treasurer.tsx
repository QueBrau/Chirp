/**
 * Treasurer per DESIGN §7: HeroCard balance (big tabular stat on the gradient),
 * dues cycle progress caption, then the append-only ledger — +/- tabular
 * amounts in success/danger with Correction/Corrected chips.
 *
 * Role-gated (SPEC §8.4/§8.2): only the chapter's treasurer or president may
 * hit the ledger/spend-approval endpoints, which the backend enforces via
 * `require_role`. The real chapter_id + role come from `GET /me/memberships`
 * (myMemberships()) rather than the mock membership, so a non-eligible user
 * sees an EmptyState instead of a wall of 403s.
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Pressable, TextInput, View } from "react-native";

import { listMembers, myMemberships, type MyMembershipOut } from "@/api/chapters";
import { ApiError } from "@/api/client";
import {
  createLedgerEntry,
  decideSpendApproval,
  exportLedgerCsv,
  listDuesCycles,
  listLedger,
  listSpendApprovals,
  type DuesCycleOut,
  type LedgerEntryOut,
  type LedgerEntryType,
  type SpendApprovalOut,
} from "@/api/finance";
import {
  AppText,
  Button,
  Card,
  Chip,
  type ChipVariant,
  EmptyState,
  HeroCard,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { shareCsv } from "@/lib/export";
import { radii, spacing, typography, useTheme } from "@/theme";

/** Entry types offered in the "Add entry" form — "correction" needs a
 * per-entry "correct this" flow (a valid corrects_entry_id) that's out of
 * scope here; offering it would just 422 against the backend. */
type FormEntryType = Exclude<LedgerEntryType, "correction">;

const ENTRY_TYPE_OPTIONS: { value: FormEntryType; label: string }[] = [
  { value: "dues_payment", label: "Dues payment" },
  { value: "expense", label: "Expense" },
  { value: "budget_allocation", label: "Budget allocation" },
  { value: "payout", label: "Payout" },
];

const SPEND_STATUS_LABEL: Record<SpendApprovalOut["status"], string> = {
  pending: "Pending",
  approved: "Approved",
  rejected: "Rejected",
};

const SPEND_STATUS_VARIANT: Record<SpendApprovalOut["status"], ChipVariant> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
};

function dollars(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function entryDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dueDate(isoDay: string): string {
  return new Date(`${isoDay}T00:00:00`).toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
  });
}

/** ApiError carries a server-provided `.detail`; anything else gets a generic fallback. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  Alert.alert(title, message);
}

/**
 * Strict dollars-string -> positive integer cents. Rejects anything that
 * doesn't parse to a finite positive amount (garbage, negative, zero, more
 * than 2 decimal places) instead of silently coercing it. Rounds rather than
 * truncates: `Math.round(parseFloat(dollars) * 100)` — truncating a value
 * like `parseFloat("12.10") * 100 === 1209.9999999999998` would bill the
 * chapter a cent short forever.
 */
function parseDollarsToCents(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return null;
  const value = parseFloat(trimmed);
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.round(value * 100);
}

function FieldLabel({ children }: { children: string }) {
  return (
    <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
      {children}
    </AppText>
  );
}

export default function TreasurerScreen() {
  const palette = useTheme();

  // undefined = /me/memberships hasn't resolved yet; null = signed-in user has
  // no treasurer/president membership anywhere (role-gated screen — NO further
  // calls, the ledger endpoints would just 403); object = the real membership
  // driving every call below.
  const [membership, setMembership] = useState<MyMembershipOut | null | undefined>(undefined);
  const [cycles, setCycles] = useState<DuesCycleOut[]>([]);
  const [ledger, setLedger] = useState<LedgerEntryOut[] | null>(null);
  const [approvals, setApprovals] = useState<SpendApprovalOut[] | null>(null);
  const [decidingId, setDecidingId] = useState<string | null>(null);
  const [exportingCsv, setExportingCsv] = useState(false);
  const [memberNames, setMemberNames] = useState<Map<string, string>>(new Map());

  // Add-entry form state.
  const [amountInput, setAmountInput] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("");
  const [entryType, setEntryType] = useState<FormEntryType>("expense");
  const [direction, setDirection] = useState<"in" | "out" | null>(null);
  const [submittingEntry, setSubmittingEntry] = useState(false);

  const chapterId = membership?.chapter_id ?? null;

  const loadDashboard = useCallback(async (id: string) => {
    const [duesCycles, entries, spendApprovals, members] = await Promise.all([
      listDuesCycles(id),
      listLedger(id),
      listSpendApprovals(id),
      listMembers(id),
    ]);
    setCycles(duesCycles);
    setLedger(entries);
    setApprovals(spendApprovals);
    // user_id -> display name. GET /chapters/{id}/members joins the name in; there
    // is no GET /users/{id}, so this roster is the only way to show who requested a
    // spend on real data.
    setMemberNames(
      new Map(
        members
          .filter((m) => m.display_name !== null)
          .map((m) => [m.user_id, m.display_name as string]),
      ),
    );
  }, []);

  useEffect(() => {
    const init = async () => {
      try {
        const memberships = await myMemberships();
        const eligible =
          memberships.find((m) => m.role === "treasurer" || m.role === "president") ?? null;
        setMembership(eligible);
        if (eligible === null) return; // role-gated: no ledger/spend-approval calls
        await loadDashboard(eligible.chapter_id);
      } catch (error) {
        showApiError(error, "Couldn't load the treasurer dashboard");
        setMembership(null);
      }
    };
    void init();
  }, [loadDashboard]);

  const entries = ledger ?? [];
  const balance = entries.reduce((sum, entry) => sum + entry.amount_cents, 0);
  // Entries that a correction points at, so both sides of a correction pair are labeled.
  const correctedIds = new Set(
    entries.map((entry) => entry.corrects_entry_id).filter((id): id is string => id !== null),
  );
  // Newest first for the ledger list (balance still sums everything).
  const sorted = [...entries].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const cycle = cycles[0];
  const paidCount =
    cycle !== undefined
      ? entries.filter(
          (entry) => entry.entry_type === "dues_payment" && entry.dues_cycle_id === cycle.id,
        ).length
      : 0;

  const sortedApprovals = [...(approvals ?? [])].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  );

  const decide = async (approval: SpendApprovalOut, status: "approved" | "rejected") => {
    if (chapterId === null || decidingId !== null) return; // double-submit guard
    setDecidingId(approval.id);
    try {
      const updated = await decideSpendApproval(chapterId, approval.id, status);
      setApprovals((current) => (current ?? []).map((a) => (a.id === updated.id ? updated : a)));
    } catch (error) {
      // A decision is one-way; 409 `already_decided` means someone else beat us
      // to it. That's a stale UI, not a real error — refetch instead of alarming.
      if (error instanceof ApiError && error.status === 409) {
        Alert.alert("Already decided", "Someone else already decided this request — refreshing.");
        try {
          setApprovals(await listSpendApprovals(chapterId));
        } catch (refetchError) {
          showApiError(refetchError, "Couldn't refresh spend approvals");
        }
      } else {
        showApiError(error, "Couldn't save that decision");
      }
    } finally {
      setDecidingId(null);
    }
  };

  const resetForm = () => {
    setAmountInput("");
    setDescription("");
    setCategory("");
    setEntryType("expense");
    setDirection(null);
  };

  const submitEntry = async (signedCents: number) => {
    if (chapterId === null || submittingEntry) return; // hard double-submit guard
    setSubmittingEntry(true);
    try {
      const created = await createLedgerEntry(chapterId, {
        entry_type: entryType,
        amount_cents: signedCents,
        category: category.trim().length > 0 ? category.trim() : null,
        description: description.trim().length > 0 ? description.trim() : null,
      });
      setLedger((current) => [created, ...(current ?? [])]);
      resetForm();
    } catch (error) {
      showApiError(error, "Couldn't add that ledger entry");
    } finally {
      setSubmittingEntry(false);
    }
  };

  const parsedCents = parseDollarsToCents(amountInput);
  const canSubmitEntry = parsedCents !== null && direction !== null && !submittingEntry;

  const confirmSubmit = () => {
    if (!canSubmitEntry || parsedCents === null || direction === null) return;
    const signedCents = direction === "out" ? -parsedCents : parsedCents;
    const typeLabel = ENTRY_TYPE_OPTIONS.find((o) => o.value === entryType)?.label ?? entryType;
    Alert.alert(
      "Add this ledger entry?",
      `${signedCents >= 0 ? "+" : ""}${dollars(signedCents)} · ${typeLabel}` +
        (description.trim().length > 0 ? `\n${description.trim()}` : "") +
        "\n\nThis entry is permanent — the ledger is append-only. It can't be edited or " +
        "deleted, only offset later by a separate correction entry.",
      [
        { text: "Cancel", style: "cancel" },
        { text: "Add entry", onPress: () => void submitEntry(signedCents) },
      ],
    );
  };

  const exportCsv = async () => {
    if (chapterId === null || exportingCsv) return;
    setExportingCsv(true);
    try {
      const csv = await exportLedgerCsv(chapterId);
      const today = new Date().toISOString().slice(0, 10);
      const filename = `${membership?.chapter_name ?? "chapter"} ledger ${today}`;
      try {
        await shareCsv(filename, csv);
      } catch {
        // expo-file-system/expo-sharing are newly-added native modules — until the
        // EAS dev build is rebuilt, shareCsv fails at native-module resolution even
        // though the CSV text above resolved fine. Surface that plainly.
        Alert.alert(
          "Can't share yet",
          "The CSV was generated, but sharing needs a native module that isn't in this " +
            "build yet. Rebuild the app (EAS dev build) and try again.",
        );
      }
    } catch (error) {
      showApiError(error, "Couldn't export the ledger");
    } finally {
      setExportingCsv(false);
    }
  };

  if (membership === null) {
    return (
      <Screen title="Treasurer" subtitle="Money in the dashboard, talk in the chat">
        <EmptyState
          title="Treasurer/president only"
          message="This dashboard is limited to the chapter's treasurer or president."
        />
      </Screen>
    );
  }

  const inputStyle = {
    ...typography.body,
    color: palette.ink,
    backgroundColor: palette.surfaceAlt,
    borderRadius: radii.input,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  };

  return (
    <Screen title="Treasurer" subtitle="Money in the dashboard, talk in the chat">
      <View style={{ gap: spacing.xl }}>
        <HeroCard>
          <View style={{ gap: spacing.sm }}>
            <AppText variant="micro" tone="onAccent">
              Chapter balance
            </AppText>
            <AppText
              variant="display"
              tone="onAccent"
              style={{ fontVariant: typography.stat.fontVariant }}
            >
              {dollars(balance)}
            </AppText>
            {cycle !== undefined ? (
              <AppText variant="caption" tone="onAccent">
                {cycle.name} · {dollars(cycle.amount_cents)} per member · {paidCount}{" "}
                {paidCount === 1 ? "payment" : "payments"} in · due {dueDate(cycle.due_date)}
              </AppText>
            ) : null}
          </View>
        </HeroCard>

        <View>
          <SectionHeader title="Spend approvals" caption="Newest first" />
          <Card>
            {approvals !== null && sortedApprovals.length === 0 ? (
              <EmptyState title="Nothing pending" message="Spend requests will show up here." />
            ) : (
              <View style={{ gap: spacing.lg }}>
                {sortedApprovals.map((approval, index) => {
                  const requesterName =
                    memberNames.get(approval.requested_by) ?? approval.requested_by;
                  const busy = decidingId === approval.id;
                  return (
                    <View
                      key={approval.id}
                      style={{
                        gap: spacing.sm,
                        paddingBottom: index < sortedApprovals.length - 1 ? spacing.lg : 0,
                        borderBottomWidth:
                          index < sortedApprovals.length - 1 ? 1 : undefined,
                        borderBottomColor: palette.border,
                      }}
                    >
                      <View
                        style={{
                          flexDirection: "row",
                          justifyContent: "space-between",
                          gap: spacing.md,
                        }}
                      >
                        <View style={{ flex: 1, gap: spacing.xs }}>
                          <AppText variant="headline">{approval.description}</AppText>
                          <AppText variant="caption" tone="secondary">
                            {requesterName} · {entryDate(approval.created_at)}
                          </AppText>
                        </View>
                        <AppText
                          variant="stat"
                          style={{ fontVariant: typography.stat.fontVariant }}
                        >
                          {dollars(approval.amount_cents)}
                        </AppText>
                      </View>
                      <View
                        style={{
                          flexDirection: "row",
                          alignItems: "center",
                          justifyContent: "space-between",
                        }}
                      >
                        <Chip
                          label={SPEND_STATUS_LABEL[approval.status]}
                          variant={SPEND_STATUS_VARIANT[approval.status]}
                        />
                        {approval.status === "pending" ? (
                          <View style={{ flexDirection: "row", gap: spacing.sm }}>
                            <Button
                              label="Reject"
                              variant="destructive"
                              disabled={busy || decidingId !== null}
                              onPress={() => void decide(approval, "rejected")}
                            />
                            <Button
                              label="Approve"
                              variant="primary"
                              disabled={busy || decidingId !== null}
                              onPress={() => void decide(approval, "approved")}
                            />
                          </View>
                        ) : null}
                      </View>
                    </View>
                  );
                })}
              </View>
            )}
          </Card>
        </View>

        <View>
          <SectionHeader title="Add entry" caption="Permanent — corrections offset, never edit" />
          <Card>
            <View style={{ gap: spacing.lg }}>
              <View>
                <FieldLabel>Money in or out</FieldLabel>
                <View style={{ flexDirection: "row", gap: spacing.sm }}>
                  {(["in", "out"] as const).map((dir) => {
                    const selected = direction === dir;
                    const tint = dir === "in" ? palette.successSoft : palette.dangerSoft;
                    const fg = dir === "in" ? palette.success : palette.danger;
                    return (
                      <Pressable
                        key={dir}
                        accessibilityRole="button"
                        accessibilityState={{ selected }}
                        onPress={() => setDirection(dir)}
                        style={{
                          flex: 1,
                          paddingVertical: spacing.md,
                          borderRadius: radii.pill,
                          alignItems: "center",
                          backgroundColor: selected ? tint : palette.surfaceAlt,
                          borderWidth: selected ? 1 : 0,
                          borderColor: fg,
                        }}
                      >
                        <AppText
                          variant="bodyBold"
                          style={{ color: selected ? fg : palette.inkSecondary }}
                        >
                          {dir === "in" ? "Money in (+)" : "Money out (−)"}
                        </AppText>
                      </Pressable>
                    );
                  })}
                </View>
              </View>

              <View>
                <FieldLabel>Amount</FieldLabel>
                <TextInput
                  value={amountInput}
                  onChangeText={setAmountInput}
                  placeholder="0.00"
                  placeholderTextColor={palette.inkFaint}
                  keyboardType="decimal-pad"
                  style={inputStyle}
                />
              </View>

              <View>
                <FieldLabel>Description</FieldLabel>
                <TextInput
                  value={description}
                  onChangeText={setDescription}
                  placeholder="e.g. Venue deposit"
                  placeholderTextColor={palette.inkFaint}
                  style={inputStyle}
                />
              </View>

              <View>
                <FieldLabel>Category</FieldLabel>
                <TextInput
                  value={category}
                  onChangeText={setCategory}
                  placeholder="e.g. rush"
                  placeholderTextColor={palette.inkFaint}
                  style={inputStyle}
                />
              </View>

              <View>
                <FieldLabel>Entry type</FieldLabel>
                <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
                  {ENTRY_TYPE_OPTIONS.map((option) => {
                    const selected = entryType === option.value;
                    return (
                      <Pressable
                        key={option.value}
                        accessibilityRole="button"
                        accessibilityState={{ selected }}
                        onPress={() => setEntryType(option.value)}
                        style={{
                          paddingVertical: spacing.sm,
                          paddingHorizontal: spacing.md,
                          borderRadius: radii.pill,
                          backgroundColor: selected ? palette.accentSoft : palette.surfaceAlt,
                        }}
                      >
                        <AppText
                          variant="bodyBold"
                          style={{ color: selected ? palette.accent : palette.inkSecondary }}
                        >
                          {option.label}
                        </AppText>
                      </Pressable>
                    );
                  })}
                </View>
              </View>

              <Button
                label={submittingEntry ? "Adding…" : "Add entry"}
                onPress={confirmSubmit}
                disabled={!canSubmitEntry}
              />
            </View>
          </Card>
        </View>

        <View>
          <SectionHeader
            title="Ledger"
            caption="Append-only — corrections are new offsetting entries"
            right={
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Export ledger as CSV"
                accessibilityState={{ disabled: exportingCsv }}
                disabled={exportingCsv}
                onPress={() => void exportCsv()}
                hitSlop={spacing.sm}
              >
                <AppText variant="bodyBold" tone={exportingCsv ? "tertiary" : "accent"}>
                  {exportingCsv ? "Exporting…" : "Export CSV"}
                </AppText>
              </Pressable>
            }
          />
          <Card>
            {ledger !== null && sorted.length === 0 ? (
              <EmptyState title="No entries yet" message="Dues and expenses land here." />
            ) : (
              sorted.map((entry, index) => (
                <ListRow
                  key={entry.id}
                  title={entry.description ?? entry.entry_type}
                  subtitle={`${entry.category ?? "uncategorized"} · ${entryDate(entry.created_at)}`}
                  divider={index < sorted.length - 1}
                  right={
                    <View style={{ alignItems: "flex-end", gap: spacing.xs }}>
                      <AppText
                        variant="stat"
                        tone={entry.amount_cents >= 0 ? "success" : "danger"}
                      >
                        {entry.amount_cents >= 0 ? "+" : ""}
                        {dollars(entry.amount_cents)}
                      </AppText>
                      {entry.entry_type === "correction" ? (
                        <Chip label="Correction" variant="accent" />
                      ) : correctedIds.has(entry.id) ? (
                        <Chip label="Corrected" variant="danger" />
                      ) : null}
                    </View>
                  }
                />
              ))
            )}
          </Card>
        </View>
      </View>
    </Screen>
  );
}

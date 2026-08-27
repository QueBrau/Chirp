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

import { useRouter } from "expo-router";
import { Feather } from "@expo/vector-icons";
import { useCallback, useEffect, useState } from "react";
import { Linking, Pressable, TextInput, View } from "react-native";

import { listMembers, myMemberships, type MyMembershipOut } from "@/api/chapters";
import { ApiError } from "@/api/client";
import { calendarDay } from "@/lib/dates";
import {
  createDuesCycle,
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
  createOnboardingLink,
  getChapterPaymentsStatus,
  type ChapterPaymentsStatus,
} from "@/api/payments";
import {
  AppText,
  BalanceTrend,
  Button,
  Card,
  CategoryDonut,
  Chip,
  type ChipVariant,
  EmptyState,
  HeroCard,
  ListRow,
  ProgressMeter,
  Screen,
  SectionHeader,
} from "@/components";
import { confirmAction, showAlert, showApiError } from "@/lib/alert";
import { shareCsv } from "@/lib/export";
import { duesProgress, runningBalance, spendByCategory } from "@/lib/treasury";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { radii, spacing, typography, useAppearance, useTheme } from "@/theme";

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

/**
 * Whole-dollar money, for chart labels only.
 *
 * Cents are load-bearing in the ledger and are always shown there. On an axis or a
 * legend they are noise that pushes the number wider than the column it sits in —
 * the exact figure is one row away in the ledger either way.
 */
function dollarsRounded(cents: number): string {
  return (cents / 100).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function entryDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dueDate(isoDay: string): string {
  return calendarDay(isoDay).toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
  });
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
  const router = useRouter();
  const palette = useTheme();
  // Campus secondary = Spartan gold (DESIGN §8.5). Used for exactly one decorative
  // mark on this screen, and deliberately NOWHERE in the charts: gold is 1.74:1 on
  // white, so a gold data mark would be close to invisible in light mode. On the
  // violet hero it is decoration on a dark ground, which is what §10.1 asks for.
  const { campusColors } = useAppearance();

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
  const [payments, setPayments] = useState<ChapterPaymentsStatus | null>(null);
  const [openingOnboarding, setOpeningOnboarding] = useState(false);

  // Add-entry form state.
  const [amountInput, setAmountInput] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("");
  const [entryType, setEntryType] = useState<FormEntryType>("expense");
  const [direction, setDirection] = useState<"in" | "out" | null>(null);
  const [submittingEntry, setSubmittingEntry] = useState(false);

  // Open-a-cycle form state (c81).
  const [cycleName, setCycleName] = useState("");
  const [cycleAmountInput, setCycleAmountInput] = useState("");
  const [cycleDueDate, setCycleDueDate] = useState("");
  const [submittingCycle, setSubmittingCycle] = useState(false);
  const [cycleError, setCycleError] = useState<string | null>(null);

  // c80 house rule: ask the server what this caller may DO, never hand-mirror
  // the treasurer/president role tuple client-side a second time. roleMeta is
  // null until it resolves, so capabilities defaults to [] and the form below
  // fails closed (absent, not a 403) exactly like every other capability gate
  // in this app.
  const { roleMeta } = useOwnChapter();
  const canOpenCycle = roleMeta?.capabilities.includes("dues_admin") ?? false;

  const chapterId = membership?.chapter_id ?? null;

  const loadDashboard = useCallback(async (id: string) => {
    const [duesCycles, entries, spendApprovals, members, paymentsStatus] = await Promise.all([
      listDuesCycles(id),
      listLedger(id),
      listSpendApprovals(id),
      listMembers(id),
      // Connect status is informational; a Stripe hiccup shouldn't blank the ledger.
      getChapterPaymentsStatus(id).catch(() => null),
    ]);
    setCycles(duesCycles);
    setLedger(entries);
    setApprovals(spendApprovals);
    setPayments(paymentsStatus);
    // user_id -> display name. GET /chapters/{id}/members joins the name in; there
    // is no GET /users/{id}, so this roster is the only way to show who requested a
    // spend on real data.
    setMemberNames(new Map(members.map((m) => [m.user_id, m.display_name])));
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

  // Chart inputs. All of these are pure functions over the ledger in src/lib/treasury,
  // kept out of the screen so the numbers behind the pictures can be tested without
  // rendering anything — a chart drawn perfectly from a wrong total is worse than no
  // chart, because it is confidently wrong about someone's money.
  const balancePoints = runningBalance(entries);
  const categorySlices = spendByCategory(entries);
  const dues = duesProgress(cycle, entries, memberNames.size);
  const paidCount = dues?.paidCount ?? 0;

  const sortedApprovals = [...(approvals ?? [])].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  );

  /** Stripe-hosted onboarding: the link is single-use and short-lived, so it's
   * fetched on tap rather than held in state. */
  const openConnectOnboarding = async () => {
    if (chapterId === null) return;
    setOpeningOnboarding(true);
    try {
      const link = await createOnboardingLink(chapterId);
      await Linking.openURL(link.url);
    } catch (error) {
      showApiError(error, "Couldn't open Stripe setup");
    } finally {
      setOpeningOnboarding(false);
    }
  };

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
        showAlert("Already decided", "Someone else already decided this request — refreshing.");
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

  // c81: open a dues cycle. amount_cents > 0 and a real date are enforced by the
  // backend schema too (Field(gt=0), a `date`), but failing the button closed
  // client-side means a treasurer never has to read a 422 to find out why nothing
  // happened.
  const parsedCycleCents = parseDollarsToCents(cycleAmountInput);
  const cycleDueDateValid = /^\d{4}-\d{2}-\d{2}$/.test(cycleDueDate.trim());
  const canSubmitCycle =
    canOpenCycle &&
    cycleName.trim().length > 0 &&
    parsedCycleCents !== null &&
    parsedCycleCents > 0 &&
    cycleDueDateValid &&
    !submittingCycle;

  const submitCycle = async () => {
    if (chapterId === null || !canSubmitCycle || parsedCycleCents === null) return;
    setSubmittingCycle(true);
    setCycleError(null);
    try {
      const created = await createDuesCycle(chapterId, {
        name: cycleName.trim(),
        amount_cents: parsedCycleCents,
        due_date: cycleDueDate.trim(),
      });
      // Newest-first, matching listDuesCycles' own ordering — the new cycle
      // becomes `cycles[0]` immediately rather than waiting on a refetch.
      setCycles((current) => [created, ...current]);
      setCycleName("");
      setCycleAmountInput("");
      setCycleDueDate("");
    } catch (error) {
      setCycleError(
        error instanceof ApiError
          ? "Couldn't open that cycle. Check the amount and date and try again."
          : "Something went wrong. Try again.",
      );
    } finally {
      setSubmittingCycle(false);
    }
  };

  const parsedCents = parseDollarsToCents(amountInput);
  const canSubmitEntry = parsedCents !== null && direction !== null && !submittingEntry;

  const confirmSubmit = () => {
    if (!canSubmitEntry || parsedCents === null || direction === null) return;
    const signedCents = direction === "out" ? -parsedCents : parsedCents;
    const typeLabel = ENTRY_TYPE_OPTIONS.find((o) => o.value === entryType)?.label ?? entryType;
    confirmAction({
      title: "Add this ledger entry?",
      message:
        `${signedCents >= 0 ? "+" : ""}${dollars(signedCents)} · ${typeLabel}` +
        (description.trim().length > 0 ? `\n${description.trim()}` : "") +
        "\n\nThis entry is permanent — the ledger is append-only. It can't be edited or " +
        "deleted, only offset later by a separate correction entry.",
      confirmLabel: "Add entry",
      onConfirm: () => void submitEntry(signedCents),
    });
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
        showAlert(
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
            {/* The one gold moment (DESIGN §10.4): short accent bar, never a wash. */}
            <View
              style={{
                width: 28,
                height: 4,
                borderRadius: radii.sm / 3,
                backgroundColor: campusColors.secondary,
              }}
            />
            {cycle !== undefined ? (
              <AppText variant="caption" tone="onAccent">
                {cycle.name} · {dollars(cycle.amount_cents)} per member · {paidCount}{" "}
                {paidCount === 1 ? "payment" : "payments"} in · due {dueDate(cycle.due_date)}
              </AppText>
            ) : null}
          </View>
        </HeroCard>

        {/* c81: open a dues cycle. Gated on the dues_admin CAPABILITY (c80's house
            rule), not a role check — a screen that already fetches its own
            treasurer/president membership for the ledger calls still has to ask the
            server what this caller may DO, because that membership fetch predates
            c80 and the two must never quietly disagree about who gets the button. */}
        {canOpenCycle ? (
          <View>
            <SectionHeader
              title="Open a cycle"
              caption={cycle !== undefined ? "Starts a new cycle — the old one stays as-is" : "No cycle yet — members can't be billed until one exists"}
            />
            <Card>
              <View style={{ gap: spacing.lg }}>
                <View>
                  <FieldLabel>Name</FieldLabel>
                  <TextInput
                    value={cycleName}
                    onChangeText={setCycleName}
                    placeholder="e.g. Fall 2026 dues"
                    placeholderTextColor={palette.inkFaint}
                    style={inputStyle}
                  />
                </View>

                <View>
                  <FieldLabel>Amount per member</FieldLabel>
                  <TextInput
                    value={cycleAmountInput}
                    onChangeText={setCycleAmountInput}
                    placeholder="0.00"
                    placeholderTextColor={palette.inkFaint}
                    keyboardType="decimal-pad"
                    style={inputStyle}
                  />
                </View>

                <View>
                  <FieldLabel>Due date</FieldLabel>
                  <TextInput
                    value={cycleDueDate}
                    onChangeText={setCycleDueDate}
                    placeholder="YYYY-MM-DD"
                    placeholderTextColor={palette.inkFaint}
                    style={inputStyle}
                  />
                </View>

                {cycleError !== null ? (
                  <AppText variant="caption" tone="danger">
                    {cycleError}
                  </AppText>
                ) : null}

                <Button
                  label={submittingCycle ? "Opening…" : "Open cycle"}
                  onPress={() => void submitCycle()}
                  disabled={!canSubmitCycle}
                />
              </View>
            </Card>
          </View>
        ) : null}

        {/* c196: installment plans for members who can't pay a cycle in one shot.
            Same dues_admin gate as "Open a cycle" above — a pushed screen, not
            another card of controls stacked here, since it has its own create
            flow and per-installment record/cancel actions. */}
        {canOpenCycle && cycle !== undefined ? (
          <View>
            <SectionHeader title="Payment plans" caption="Installments for members who can't pay at once" />
            <Card onPress={() => router.push("/chapter/dues-plans")}>
              <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.lg }}>
                <Feather name="calendar" size={typography.title.fontSize} color={palette.accent} />
                <View style={{ flex: 1, gap: spacing.xs }}>
                  <AppText variant="headline">Manage payment plans</AppText>
                  <AppText variant="caption" tone="secondary">
                    Set up installments and record payments against them
                  </AppText>
                </View>
                <Feather name="chevron-right" size={typography.title.fontSize} color={palette.inkFaint} />
              </View>
            </Card>
          </View>
        ) : null}

        {/*
          ONE zone, not three more cards on the stack (DESIGN §10.1: zones, not card
          soup). Everything in here is derived from the same ledger rendered further
          down — none of it is a second source of truth, which is why the section
          caption says so rather than implying a separate report.

          Each block is a deliberately different shape: a wide short band, a single
          bar, then a ring beside a list. Identical spacing on identical rectangles is
          the tell §10.3 is about.
        */}
        {entries.length > 0 ? (
          <View style={{ gap: spacing.md }}>
            <SectionHeader
              title="Where the money stands"
              caption="All of it derived from the ledger below, not a separate report"
            />

            <Card>
              <AppText variant="title">Balance over time</AppText>
              <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.xs }}>
                {`${entries.length} ${entries.length === 1 ? "entry" : "entries"} · running total, oldest to newest`}
              </AppText>
              <View style={{ marginTop: spacing.md }}>
                <BalanceTrend points={balancePoints} format={dollarsRounded} />
              </View>
            </Card>

            {/* Only when there is a target to measure against. Without a roster the
                denominator is unknown, and a meter drawn against a guess is a lie
                with a progress bar around it. */}
            {dues !== null && dues.expectedCents > 0 ? (
              <Card>
                <AppText variant="title">Dues collected</AppText>
                <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.xs }}>
                  {`${cycle?.name ?? "This cycle"} · ${dues.paidCount} of ${dues.memberCount} members paid`}
                </AppText>
                <View style={{ marginTop: spacing.md }}>
                  <ProgressMeter
                    fraction={dues.fraction}
                    label={`Dues collected: ${dollars(dues.collectedCents)} of ${dollars(dues.expectedCents)}`}
                  />
                  <View
                    style={{
                      flexDirection: "row",
                      alignItems: "baseline",
                      justifyContent: "space-between",
                    }}
                  >
                    <AppText
                      variant="stat"
                      style={{ fontVariant: typography.stat.fontVariant }}
                    >
                      {dollars(dues.collectedCents)}
                    </AppText>
                    <AppText variant="caption" tone="tertiary">
                      {`of ${dollars(dues.expectedCents)}`}
                    </AppText>
                  </View>
                  {dues.overCollected ? (
                    <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.xs }}>
                      {`More came in than this cycle bills — the bar is capped at full, the figure is not.`}
                    </AppText>
                  ) : null}
                </View>
              </Card>
            ) : null}

            {/* Spending only. A chapter that has taken money in but not spent any has
                no part-to-whole to show, and an empty ring is worse than no ring. */}
            {categorySlices.length > 0 ? (
              <Card>
                <AppText variant="title">Where the money went</AppText>
                <AppText variant="caption" tone="secondary" style={{ marginTop: spacing.xs }}>
                  {`Across ${categorySlices.length} ${categorySlices.length === 1 ? "category" : "categories"}`}
                </AppText>
                <View style={{ marginTop: spacing.md }}>
                  <CategoryDonut
                    slices={categorySlices}
                    format={dollarsRounded}
                    centerCaption="total out"
                  />
                </View>
              </Card>
            ) : null}
          </View>
        ) : null}

        <View>
          <SectionHeader
            title="Card & bank payments"
            caption="Dues land in the chapter's Stripe balance"
          />
          <Card>
            <View style={{ gap: spacing.md }}>
              {payments?.onboarded ? (
                <>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
                    <Chip label="Active" variant="success" />
                    <AppText variant="body" style={{ flex: 1 }}>
                      Members can pay dues in the app.
                    </AppText>
                  </View>
                  <AppText variant="caption" tone="tertiary">
                    Chirp takes 1% on card and 2% on bank transfers. Chargebacks are the
                    chapter&apos;s — Chirp never holds your money.
                  </AppText>
                </>
              ) : (
                <>
                  <AppText variant="body">
                    {payments?.details_submitted
                      ? "Stripe is still reviewing the chapter's details. Payments switch on automatically once it clears."
                      : "Finish Stripe setup to collect dues by card or bank transfer."}
                  </AppText>
                  <Button
                    label={openingOnboarding ? "Opening…" : "Set up payments"}
                    onPress={() => void openConnectOnboarding()}
                    disabled={openingOnboarding}
                  />
                </>
              )}
            </View>
          </Card>
        </View>

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

/**
 * Payment plans (board card c196, backend c195): treasurer/president set up and
 * manage installment plans for members who can't pay a dues cycle in one shot.
 *
 * Reached from BOTH treasurer.tsx and president.tsx as a pushed screen — a nav
 * card in each, matching the repo's tool-tile idiom (chapter/index.tsx's
 * OrgToolsSegment). Gated on the dues_admin CAPABILITY (c80's house rule), not a
 * role list: DUES_ADMIN is {treasurer, president} on the backend, the same tuple
 * create_dues_cycle already gates on, so this screen asks the server the exact
 * same question treasurer.tsx's "Open a cycle" card does.
 *
 * The eligibility check ("may I even set up a plan before hitting 409") happens
 * first, unconditionally, before any loading state — mirroring president.tsx's
 * isPresident gate exactly (see that file's comment): roleMeta is null while
 * loading OR on a failed fetch, and this app fails CLOSED on that ambiguity
 * rather than ever flashing an officer screen before eligibility is confirmed.
 */

import { Feather } from "@expo/vector-icons";
import { useCallback, useEffect, useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import { listMembers, type MemberOut } from "@/api/chapters";
import { ApiError } from "@/api/client";
import {
  cancelDuesPaymentPlan,
  createDuesPaymentPlan,
  listDuesCycles,
  listDuesPaymentPlans,
  recordDuesInstallmentPayment,
  type DuesCycleOut,
  type DuesPaymentPlanOut,
  type DuesPaymentPlanStatus,
  type DuesPlanInstallmentOut,
} from "@/api/finance";
import {
  AppText,
  Button,
  Card,
  Chip,
  type ChipVariant,
  EmptyState,
  ProgressMeter,
  Screen,
  SectionHeader,
} from "@/components";
import { confirmAction, showAlert, showApiError } from "@/lib/alert";
import { calendarDay } from "@/lib/dates";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { radii, spacing, typography, useTheme } from "@/theme";

const STATUS_LABEL: Record<DuesPaymentPlanStatus, string> = {
  active: "Active",
  completed: "Completed",
  canceled: "Canceled",
};

const STATUS_VARIANT: Record<DuesPaymentPlanStatus, ChipVariant> = {
  active: "accent",
  completed: "success",
  canceled: "danger",
};

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const MAX_INSTALLMENTS = 24;

function dollars(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function dueDateLabel(isoDay: string): string {
  return calendarDay(isoDay).toLocaleDateString(undefined, { month: "long", day: "numeric" });
}

/** paid_at is a real instant (the moment record_dues_installment_payment ran), not
 * a bare calendar day, so it renders directly rather than through calendarDay() —
 * same split src/payments/dues.tsx's installmentPaidDate documents. */
function recordedDateLabel(isoInstant: string): string {
  return new Date(isoInstant).toLocaleDateString(undefined, { month: "long", day: "numeric" });
}

function isoDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** Local-calendar day arithmetic (see @/lib/dates) — setDate rolls month/year
 * correctly and, unlike adding raw milliseconds, is unaffected by DST shifts. */
function addDays(date: Date, days: number): Date {
  const result = new Date(date);
  result.setDate(result.getDate() + days);
  return result;
}

/**
 * Strict dollars-string -> positive integer cents. Same rules as treasurer.tsx's
 * parseDollarsToCents (kept as a local copy, not a shared import, so this screen's
 * client additions stay self-contained for c197's parallel rebase): rejects
 * garbage/negative/zero/more-than-2-decimals, rounds rather than truncates.
 */
function parseDollarsToCents(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(trimmed)) return null;
  const value = parseFloat(trimmed);
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.round(value * 100);
}

interface DraftInstallment {
  amountInput: string;
  dueDate: string;
}

/**
 * Even split of `totalCents` across `count` installments, as a starting point —
 * every amount and date stays editable, and the server is the actual authority on
 * the sum (422 installments_must_sum_to_cycle_amount otherwise). The last
 * installment absorbs the remainder cents so the suggested sum is always exactly
 * totalCents. Dates spread from today to the cycle's due date; if that date has
 * already passed, spread at 30-day intervals instead so the suggestion is still
 * usable rather than backdated.
 */
function suggestInstallments(
  totalCents: number,
  cycleDueDate: string,
  count: number,
): DraftInstallment[] {
  if (count <= 0) return [];
  const base = Math.floor(totalCents / count);
  const remainder = totalCents - base * count;
  const today = new Date();
  const rawSpanDays = Math.round(
    (calendarDay(cycleDueDate).getTime() - today.getTime()) / 86_400_000,
  );
  const spanDays = rawSpanDays > 0 ? rawSpanDays : count * 30;

  return Array.from({ length: count }, (_, index) => {
    const seq = index + 1;
    const amountCents = seq === count ? base + remainder : base;
    // max(seq, ...) keeps installments on strictly increasing days even when
    // spanDays is smaller than count.
    const offsetDays = Math.max(seq, Math.round((seq / count) * spanDays));
    return {
      amountInput: (amountCents / 100).toFixed(2),
      dueDate: isoDate(addDays(today, offsetDays)),
    };
  });
}

/** Friendly copy for the 409/422 codes create_dues_payment_plan can return — the
 * server is the authority on all of these, this just translates the code. */
function planCreateErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.detail) {
      case "already_paid":
        return "This member has already paid this cycle in full, so no plan is needed.";
      case "on_payment_plan":
        return "This member already has an active payment plan for this cycle.";
      case "payment_in_progress":
        return "This member has a payment in progress for this cycle. Wait for it to resolve, then try again.";
      case "installments_must_sum_to_cycle_amount":
        return "Those amounts don't add up to the cycle total. Check the installments and try again.";
      case "installment_count_mismatch":
        return "Something went wrong building the installment list. Try again.";
      case "membership_not_found":
        return "That member isn't active in this chapter anymore.";
      case "dues_cycle_not_found":
        return "This dues cycle couldn't be found. Refresh and try again.";
      default:
        return error.detail;
    }
  }
  return "Something went wrong. Try again.";
}

function FieldLabel({ children }: { children: string }) {
  return (
    <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
      {children}
    </AppText>
  );
}

/** Create-plan form: member picker, installment count, auto-suggested + editable
 * amounts/dates, client-side sum check before the POST (the server also 422s). */
function CreatePlanForm({
  cycle,
  eligibleMembers,
  membersLoaded,
  onCreate,
  onCancel,
}: {
  cycle: DuesCycleOut;
  eligibleMembers: MemberOut[];
  membersLoaded: boolean;
  onCreate: (
    body: Parameters<typeof createDuesPaymentPlan>[2],
  ) => Promise<DuesPaymentPlanOut>;
  onCancel: () => void;
}) {
  const palette = useTheme();
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [countInput, setCountInput] = useState("2");
  const [installments, setInstallments] = useState<DraftInstallment[]>(() =>
    suggestInstallments(cycle.amount_cents, cycle.due_date, 2),
  );
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const onChangeCount = (text: string) => {
    setCountInput(text);
    const parsed = parseInt(text, 10);
    if (Number.isInteger(parsed) && parsed > 0 && parsed <= MAX_INSTALLMENTS) {
      setInstallments(suggestInstallments(cycle.amount_cents, cycle.due_date, parsed));
    }
  };

  const updateInstallment = (index: number, patch: Partial<DraftInstallment>) => {
    setInstallments((current) =>
      current.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    );
  };

  const parsedAmounts = installments.map((item) => parseDollarsToCents(item.amountInput));
  const installmentsValid =
    installments.length > 0 &&
    parsedAmounts.every((cents) => cents !== null) &&
    installments.every((item) => ISO_DATE_RE.test(item.dueDate.trim()));
  const sumCents = parsedAmounts.reduce<number>((sum, cents) => sum + (cents ?? 0), 0);
  const sumMatches = sumCents === cycle.amount_cents;
  const canSubmit =
    selectedUserId !== null && installmentsValid && sumMatches && !submitting;

  const inputStyle = {
    ...typography.body,
    color: palette.ink,
    backgroundColor: palette.surfaceAlt,
    borderRadius: radii.input,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  };
  const smallInputStyle = { ...inputStyle, paddingVertical: spacing.sm, flex: 1 };

  const submit = async () => {
    if (!canSubmit || selectedUserId === null) return;
    setSubmitting(true);
    setFormError(null);
    try {
      await onCreate({
        user_id: selectedUserId,
        installment_count: installments.length,
        note: note.trim().length > 0 ? note.trim() : null,
        installments: installments.map((item) => ({
          amount_cents: parseDollarsToCents(item.amountInput) as number,
          due_date: item.dueDate.trim(),
        })),
      });
    } catch (error) {
      setFormError(planCreateErrorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Card>
      <View style={{ gap: spacing.lg }}>
        <View>
          <FieldLabel>Member</FieldLabel>
          {membersLoaded && eligibleMembers.length === 0 ? (
            <AppText variant="caption" tone="secondary">
              Every active member already has a plan or is fully paid for this cycle.
            </AppText>
          ) : (
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
              {eligibleMembers.map((member) => {
                const selected = selectedUserId === member.user_id;
                return (
                  <Pressable
                    key={member.id}
                    accessibilityRole="button"
                    accessibilityState={{ selected }}
                    onPress={() => setSelectedUserId(member.user_id)}
                    style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
                  >
                    <Chip
                      label={member.display_name || member.user_id}
                      variant={selected ? "accent" : "neutral"}
                    />
                  </Pressable>
                );
              })}
            </View>
          )}
        </View>

        <View>
          <FieldLabel>Number of installments</FieldLabel>
          <TextInput
            value={countInput}
            onChangeText={onChangeCount}
            keyboardType="number-pad"
            placeholder="2"
            placeholderTextColor={palette.inkFaint}
            style={inputStyle}
          />
        </View>

        <View style={{ gap: spacing.md }}>
          <FieldLabel>Installments, amount and due date, editable</FieldLabel>
          {installments.map((item, index) => (
            <View key={index} style={{ gap: spacing.xs }}>
              <AppText variant="caption" tone="tertiary">
                {`Installment ${index + 1}`}
              </AppText>
              <View style={{ flexDirection: "row", gap: spacing.sm }}>
                <TextInput
                  value={item.amountInput}
                  onChangeText={(text) => updateInstallment(index, { amountInput: text })}
                  keyboardType="decimal-pad"
                  placeholder="0.00"
                  placeholderTextColor={palette.inkFaint}
                  style={smallInputStyle}
                />
                <TextInput
                  value={item.dueDate}
                  onChangeText={(text) => updateInstallment(index, { dueDate: text })}
                  placeholder="YYYY-MM-DD"
                  placeholderTextColor={palette.inkFaint}
                  style={smallInputStyle}
                />
              </View>
            </View>
          ))}
          <View
            style={{
              flexDirection: "row",
              justifyContent: "space-between",
              paddingTop: spacing.xs,
            }}
          >
            <AppText variant="caption" tone={sumMatches ? "secondary" : "danger"}>
              {`${dollars(sumCents)} of ${dollars(cycle.amount_cents)}`}
            </AppText>
            {!sumMatches ? (
              <AppText variant="caption" tone="danger">
                Must equal the cycle total
              </AppText>
            ) : null}
          </View>
        </View>

        <View>
          <FieldLabel>Note (optional)</FieldLabel>
          <TextInput
            value={note}
            onChangeText={setNote}
            placeholder="e.g. approved by e-board 9/1"
            placeholderTextColor={palette.inkFaint}
            style={inputStyle}
          />
        </View>

        {formError !== null ? (
          <AppText variant="caption" tone="danger">
            {formError}
          </AppText>
        ) : null}

        <View style={{ flexDirection: "row", gap: spacing.sm }}>
          <Button label="Cancel" variant="ghost" onPress={onCancel} style={{ flex: 1 }} />
          <Button
            label={submitting ? "Creating…" : "Create plan"}
            onPress={() => void submit()}
            disabled={!canSubmit}
            style={{ flex: 1 }}
          />
        </View>
      </View>
    </Card>
  );
}

/** One plan's card: progress, expandable installment rows, record-payment and
 * cancel actions. */
function PlanCard({
  plan,
  memberName,
  onRecord,
  onCancelPlan,
  recordingId,
  cancelingId,
}: {
  plan: DuesPaymentPlanOut;
  memberName: string;
  onRecord: (plan: DuesPaymentPlanOut, installment: DuesPlanInstallmentOut, note: string) => void;
  onCancelPlan: (plan: DuesPaymentPlanOut) => void;
  recordingId: string | null;
  cancelingId: string | null;
}) {
  const palette = useTheme();
  const [expanded, setExpanded] = useState(false);
  const [noteDrafts, setNoteDrafts] = useState<Record<string, string>>({});

  // c235: how much is actually collected is effective_paid, not paid_at. paid_at
  // is write-once history, so a refunded installment kept inflating this meter and
  // the "N of M installments paid" line while the member's own screen and the
  // ledger both said the money had gone back out.
  const paid = plan.installments.filter((i) => i.effective_paid);
  const paidCents = paid.reduce((sum, i) => sum + i.amount_cents, 0);
  const fraction = plan.total_cents > 0 ? Math.min(paidCents / plan.total_cents, 1) : 0;

  return (
    <Card>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        onPress={() => setExpanded((v) => !v)}
        style={{ gap: spacing.md }}
      >
        <View
          style={{
            flexDirection: "row",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: spacing.md,
          }}
        >
          <View style={{ flex: 1, gap: spacing.xs }}>
            <AppText variant="headline">{memberName}</AppText>
            <AppText variant="caption" tone="secondary">
              {`${paid.length} of ${plan.installment_count} installments paid`}
              {plan.note ? ` · ${plan.note}` : ""}
            </AppText>
          </View>
          <View style={{ alignItems: "flex-end", gap: spacing.xs }}>
            <Chip label={STATUS_LABEL[plan.status]} variant={STATUS_VARIANT[plan.status]} />
            <Feather
              name={expanded ? "chevron-up" : "chevron-down"}
              size={16}
              color={palette.inkFaint}
            />
          </View>
        </View>

        <View>
          <ProgressMeter
            fraction={fraction}
            label={`${dollars(paidCents)} of ${dollars(plan.total_cents)} paid`}
          />
          <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
            <AppText
              variant="stat"
              style={{ fontVariant: typography.stat.fontVariant }}
            >
              {dollars(paidCents)}
            </AppText>
            <AppText variant="caption" tone="tertiary">
              {`of ${dollars(plan.total_cents)}`}
            </AppText>
          </View>
        </View>
      </Pressable>

      {expanded ? (
        <View style={{ marginTop: spacing.lg, gap: spacing.md }}>
          {plan.installments.map((installment, index) => {
            const busy = recordingId === installment.id;
            return (
              <View
                key={installment.id}
                style={{
                  gap: spacing.sm,
                  paddingTop: spacing.md,
                  borderTopWidth: index > 0 ? 1 : 0,
                  borderTopColor: palette.border,
                }}
              >
                <View
                  style={{
                    flexDirection: "row",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <View style={{ gap: 2 }}>
                    <AppText variant="bodyBold">{`Installment ${installment.seq}`}</AppText>
                    <AppText variant="caption" tone="secondary">
                      {`${dollars(installment.amount_cents)} · due ${dueDateLabel(installment.due_date)}`}
                    </AppText>
                  </View>
                  {installment.effective_paid ? (
                    <Chip label="Paid" variant="success" />
                  ) : installment.paid_at !== null ? (
                    // c235: recorded once, then corrected back out. A "Paid" chip
                    // here was the treasurer half of the two-screens-disagree bug:
                    // this screen claimed collected while the ledger said refunded.
                    <Chip label="Refunded" variant="warning" />
                  ) : null}
                </View>

                {/* c235: this gate stays on paid_at DELIBERATELY, while every paid/unpaid
                    STATUS above now reads effective_paid. The backend claims an installment
                    with `UPDATE ... WHERE paid_at IS NULL` and 409s installment_already_paid
                    otherwise, so a refunded installment (paid_at set, effective_paid false)
                    can never be recorded again. Gating this on effective_paid would put a
                    Record payment button on a request that is guaranteed to fail. */}
                {installment.paid_at === null && plan.status === "active" ? (
                  <View style={{ gap: spacing.sm }}>
                    <TextInput
                      value={noteDrafts[installment.id] ?? ""}
                      onChangeText={(text) =>
                        setNoteDrafts((current) => ({ ...current, [installment.id]: text }))
                      }
                      placeholder="Note (e.g. cash, venmo)"
                      placeholderTextColor={palette.inkFaint}
                      style={{
                        ...typography.body,
                        color: palette.ink,
                        backgroundColor: palette.surfaceAlt,
                        borderRadius: radii.input,
                        paddingHorizontal: spacing.md,
                        paddingVertical: spacing.sm,
                      }}
                    />
                    <Button
                      label={busy ? "Recording…" : "Record payment"}
                      variant="secondary"
                      disabled={busy || recordingId !== null}
                      onPress={() =>
                        onRecord(plan, installment, (noteDrafts[installment.id] ?? "").trim())
                      }
                    />
                  </View>
                ) : installment.paid_at !== null && !installment.effective_paid ? (
                  // c235: the row above now correctly reads Refunded, which leaves a
                  // treasurer looking at an unpaid installment with no way to record
                  // it. Say why here instead of showing a button that would 409.
                  <AppText variant="caption" tone="warning">
                    {`Recorded ${recordedDateLabel(installment.paid_at)}, then refunded. An installment can only be recorded once, so add any replacement payment to the ledger.`}
                  </AppText>
                ) : null}
              </View>
            );
          })}

          {plan.status === "active" ? (
            <Button
              label={cancelingId === plan.id ? "Canceling…" : "Cancel plan"}
              variant="destructive"
              disabled={cancelingId !== null}
              onPress={() => onCancelPlan(plan)}
              style={{ marginTop: spacing.sm }}
            />
          ) : null}
        </View>
      ) : null}
    </Card>
  );
}

export default function DuesPlansScreen() {
  const { membership, roleMeta } = useOwnChapter();
  // c80: ask the server what this caller may DO — same DUES_ADMIN gate
  // treasurer.tsx's "Open a cycle" card checks, since create_dues_payment_plan
  // and every other route on this screen require exactly that capability.
  const canManagePlans = roleMeta?.capabilities.includes("dues_admin") ?? false;
  const chapterId = membership?.chapter_id ?? null;

  const [cycles, setCycles] = useState<DuesCycleOut[]>([]);
  const [plans, setPlans] = useState<DuesPaymentPlanOut[] | null>(null);
  const [members, setMembers] = useState<MemberOut[]>([]);
  const [loaded, setLoaded] = useState(false);
  // c313: a failed init must never render as "No dues cycle yet" - that copy
  // tells a treasurer to open a cycle that may already exist.
  const [loadFailed, setLoadFailed] = useState(false);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [recordingId, setRecordingId] = useState<string | null>(null);
  const [cancelingId, setCancelingId] = useState<string | null>(null);

  const cycle = cycles[0];

  const load = useCallback(async (id: string, cycleId: string) => {
    const [plansResult, membersResult] = await Promise.all([
      listDuesPaymentPlans(id, cycleId),
      listMembers(id),
    ]);
    setPlans(plansResult);
    setMembers(membersResult);
  }, []);

  // Hoisted (c313) so the failure state's Try again can rerun it; retryKey
  // re-triggers the effect, which keeps the cancelled-cleanup semantics intact.
  const [retryKey, setRetryKey] = useState(0);
  useEffect(() => {
    if (chapterId === null || !canManagePlans) return;
    let cancelled = false;
    const init = async () => {
      setLoadFailed(false);
      try {
        const duesCycles = await listDuesCycles(chapterId);
        if (cancelled) return;
        setCycles(duesCycles);
        if (duesCycles[0]) await load(chapterId, duesCycles[0].id);
      } catch (error) {
        if (!cancelled) {
          // c313: mark the failure instead of falling through to states that
          // read as facts ("No dues cycle yet" / "No payment plans yet").
          setLoadFailed(true);
          showApiError(error, "Couldn't load payment plans");
        }
      } finally {
        if (!cancelled) setLoaded(true);
      }
    };
    void init();
    return () => {
      cancelled = true;
    };
  }, [chapterId, canManagePlans, load, retryKey]);

  const memberName = (userId: string) =>
    members.find((m) => m.user_id === userId)?.display_name || userId;

  // Cheap client-side filter, not the authority: excludes members who already
  // have a non-canceled plan for this cycle (active OR completed, since a
  // completed plan leaves dues_installment ledger rows that would 409
  // already_paid). The server's 409s (already_paid / on_payment_plan /
  // payment_in_progress) are still what actually enforces this.
  const ineligibleUserIds = new Set(
    (plans ?? []).filter((p) => p.status !== "canceled").map((p) => p.user_id),
  );
  const eligibleMembers =
    plans === null
      ? []
      : members.filter((m) => m.status === "active" && !ineligibleUserIds.has(m.user_id));

  const createPlan = async (
    body: Parameters<typeof createDuesPaymentPlan>[2],
  ): Promise<DuesPaymentPlanOut> => {
    if (chapterId === null || cycle === undefined) throw new Error("no chapter/cycle");
    const created = await createDuesPaymentPlan(chapterId, cycle.id, body);
    setPlans((current) => [created, ...(current ?? [])]);
    setShowCreateForm(false);
    return created;
  };

  const recordInstallment = async (
    plan: DuesPaymentPlanOut,
    installment: DuesPlanInstallmentOut,
    note: string,
  ) => {
    if (chapterId === null || recordingId !== null) return;
    setRecordingId(installment.id);
    try {
      const updated = await recordDuesInstallmentPayment(
        chapterId,
        plan.id,
        installment.seq,
        note.length > 0 ? note : null,
      );
      setPlans((current) =>
        (current ?? []).map((p) => {
          if (p.id !== plan.id) return p;
          const nextInstallments = p.installments.map((i) =>
            i.id === updated.id ? updated : i,
          );
          // c235: paid_at ON PURPOSE, unlike the paid/unpaid rendering above. This
          // mirrors the server's own completion rule (record_dues_installment_payment
          // flips the plan to completed when no installment has paid_at IS NULL), so
          // reading effective_paid here would make this optimistic status disagree
          // with what the same request just decided, and a refund could silently
          // reopen a plan the backend still considers completed.
          const allPaid = nextInstallments.every((i) => i.paid_at !== null);
          return { ...p, installments: nextInstallments, status: allPaid ? "completed" : p.status };
        }),
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        showAlert(
          "Couldn't record that payment",
          error.detail === "plan_not_active"
            ? "This plan is no longer active. It may have been completed or canceled."
            : "Someone already recorded this installment. Refreshing.",
        );
        if (cycle !== undefined) await load(chapterId, cycle.id);
      } else {
        showApiError(error, "Couldn't record that payment");
      }
    } finally {
      setRecordingId(null);
    }
  };

  const confirmRecord = (
    plan: DuesPaymentPlanOut,
    installment: DuesPlanInstallmentOut,
    note: string,
  ) => {
    confirmAction({
      title: `Mark installment ${installment.seq} of ${plan.installment_count} paid?`,
      message:
        `${dollars(installment.amount_cents)} · due ${dueDateLabel(installment.due_date)}` +
        (note.length > 0 ? `\nNote: ${note}` : "") +
        "\n\nThis appends a permanent ledger entry. It can't be undone, only offset later by a correction.",
      confirmLabel: "Record payment",
      onConfirm: () => void recordInstallment(plan, installment, note),
    });
  };

  const cancelPlan = async (plan: DuesPaymentPlanOut) => {
    if (chapterId === null || cancelingId !== null) return;
    setCancelingId(plan.id);
    try {
      const updated = await cancelDuesPaymentPlan(chapterId, plan.id);
      setPlans((current) => (current ?? []).map((p) => (p.id === updated.id ? updated : p)));
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        showAlert("Already settled", "This plan was already completed or canceled. Refreshing.");
        if (cycle !== undefined) await load(chapterId, cycle.id);
      } else {
        showApiError(error, "Couldn't cancel that plan");
      }
    } finally {
      setCancelingId(null);
    }
  };

  const confirmCancelPlan = (plan: DuesPaymentPlanOut) => {
    confirmAction({
      title: `Cancel ${memberName(plan.user_id)}'s payment plan?`,
      message: "Remaining unpaid installments are no longer collectible through this plan.",
      confirmLabel: "Cancel plan",
      cancelLabel: "Keep it",
      destructive: true,
      onConfirm: () => void cancelPlan(plan),
    });
  };

  // Fails CLOSED on the loading/no-capability ambiguity, matching president.tsx's
  // isPresident gate exactly — see this file's top comment.
  if (!canManagePlans) {
    return (
      <Screen title="Payment plans" subtitle="Installment plans for dues">
        <EmptyState
          title="Treasurer/president only"
          message="Payment plans are limited to the chapter's treasurer or president."
        />
      </Screen>
    );
  }

  if (chapterId === null || !loaded) {
    return (
      <Screen title="Payment plans" subtitle="Installment plans for dues">
        <EmptyState title="Loading…" />
      </Screen>
    );
  }

  // c313: the failure gate sits BEFORE the no-cycle gate, because "No dues
  // cycle yet - open one" said to a treasurer on a network blip is an
  // instruction to create a duplicate of a cycle that may already exist.
  if (loadFailed) {
    return (
      <Screen title="Payment plans" subtitle="Installment plans for dues">
        <EmptyState
          title="Couldn't load payment plans"
          message="Something went wrong reaching the server. Nothing here says whether a cycle or plan exists."
          actionLabel="Try again"
          onAction={() => setRetryKey((k) => k + 1)}
        />
      </Screen>
    );
  }

  if (cycle === undefined) {
    return (
      <Screen title="Payment plans" subtitle="Installment plans for dues">
        <EmptyState
          title="No dues cycle yet"
          message="Open a dues cycle from Treasurer before setting up a payment plan."
        />
      </Screen>
    );
  }

  const sortedPlans = [...(plans ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at));

  return (
    <Screen title="Payment plans" subtitle={cycle.name}>
      <View style={{ gap: spacing.xl }}>
        <SectionHeader
          title={cycle.name}
          caption={`${dollars(cycle.amount_cents)} per member · due ${dueDateLabel(cycle.due_date)}`}
          right={
            !showCreateForm ? (
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="New payment plan"
                onPress={() => setShowCreateForm(true)}
                hitSlop={spacing.sm}
              >
                <AppText variant="bodyBold" tone="accent">
                  New plan
                </AppText>
              </Pressable>
            ) : undefined
          }
        />

        {showCreateForm ? (
          <CreatePlanForm
            cycle={cycle}
            eligibleMembers={eligibleMembers}
            membersLoaded={plans !== null}
            onCreate={createPlan}
            onCancel={() => setShowCreateForm(false)}
          />
        ) : null}

        {sortedPlans.length === 0 ? (
          <EmptyState
            title="No payment plans yet"
            message="Set one up for a member who can't pay this cycle all at once."
          />
        ) : (
          <View style={{ gap: spacing.md }}>
            {sortedPlans.map((plan) => (
              <PlanCard
                key={plan.id}
                plan={plan}
                memberName={memberName(plan.user_id)}
                onRecord={confirmRecord}
                onCancelPlan={confirmCancelPlan}
                recordingId={recordingId}
                cancelingId={cancelingId}
              />
            ))}
          </View>
        )}
      </View>
    </Screen>
  );
}

/**
 * Member dues: what you owe this chapter, and the PaymentSheet flow to settle it.
 *
 * A cycle counts as paid when a dues_payment ledger entry exists for (cycle, member).
 * That entry is written by the Stripe webhook, never by the client — so on the ACH
 * rail a cycle stays unpaid here until the transfer actually clears, which is the
 * honest thing to show.
 */

import { useCallback, useEffect, useState } from "react";
import { Linking, View } from "react-native";

import { myMemberships, type MyMembershipOut } from "@/api/chapters";
import {
  getMyPlan,
  listDuesCycles,
  listLedger,
  type DuesCycleOut,
  type DuesPaymentPlanOut,
} from "@/api/finance";
import { getChapterPaymentsStatus } from "@/api/payments";
import { AppText, Card, Chip, EmptyState, Screen, SectionHeader } from "@/components";
import { calendarDay } from "@/lib/dates";
import DuesPaymentScreen, { PlanProgressCard } from "@/payments/dues";
import { spacing } from "@/theme";

function dollars(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function dueDate(isoDay: string): string {
  return calendarDay(isoDay).toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
  });
}

export default function DuesScreen() {
  const [membership, setMembership] = useState<MyMembershipOut | null | undefined>(undefined);
  const [cycles, setCycles] = useState<DuesCycleOut[]>([]);
  const [cyclesFailed, setCyclesFailed] = useState(false);
  const [paidCycleIds, setPaidCycleIds] = useState<Set<string>>(new Set());
  const [acceptsPayments, setAcceptsPayments] = useState(false);
  // Keyed by dues_cycle_id. Only ever holds a plan when one exists (c197) — a
  // cycle with no plan at all just has no entry, same "absence over sentinel"
  // shape as paidCycleIds above.
  const [planByCycle, setPlanByCycle] = useState<Map<string, DuesPaymentPlanOut>>(new Map());

  const load = useCallback(async (chapterId: string, userId: string) => {
    const [duesCycles, ledger, status] = await Promise.all([
      // null, not [] — a failed load must stay distinguishable from a genuinely
      // empty cycle list, or a member who owes money is told "Nothing due".
      listDuesCycles(chapterId).catch(() => null),
      listLedger(chapterId).catch(() => []),
      getChapterPaymentsStatus(chapterId).catch(() => ({
        onboarded: false,
        charges_enabled: false,
        details_submitted: false,
      })),
    ]);
    setCyclesFailed(duesCycles === null);
    const cyclesList = duesCycles ?? [];
    setCycles(cyclesList);
    setAcceptsPayments(status.onboarded);
    setPaidCycleIds(
      new Set(
        ledger
          .filter(
            (entry) =>
              entry.entry_type === "dues_payment" &&
              entry.related_user_id === userId &&
              entry.dues_cycle_id !== null,
          )
          .map((entry) => entry.dues_cycle_id as string),
      ),
    );

    // One request per cycle (not per member — this is always "my" plans), so no
    // N+1 growth with roster size the way c181's directory warning was about.
    // A per-cycle failure (e.g. a transient network blip) just leaves that cycle
    // without a plan entry rather than failing the whole screen.
    const plans = await Promise.all(
      cyclesList.map((cycle) => getMyPlan(chapterId, cycle.id).catch(() => null)),
    );
    const nextPlanByCycle = new Map<string, DuesPaymentPlanOut>();
    cyclesList.forEach((cycle, index) => {
      const plan = plans[index];
      if (plan !== null) nextPlanByCycle.set(cycle.id, plan);
    });
    setPlanByCycle(nextPlanByCycle);
  }, []);

  useEffect(() => {
    const init = async () => {
      const memberships = await myMemberships().catch(() => []);
      const active = memberships[0] ?? null;
      setMembership(active);
      if (active) await load(active.chapter_id, active.user_id);
    };
    void init();
  }, [load]);

  if (membership === undefined) {
    return (
      <Screen title="Dues">
        <EmptyState title="Loading dues…" />
      </Screen>
    );
  }

  if (membership === null) {
    return (
      <Screen title="Dues">
        <EmptyState
          title="Join a chapter first"
          message="Dues show up here once you're an active member of a chapter."
        />
      </Screen>
    );
  }

  // A completed plan has no single dues_payment ledger row to key paidCycleIds
  // off of — record_dues_installment_payment appends one dues_installment entry
  // per installment instead (see @/api/finance getMyPlan's doc comment) — so a
  // plan reaching "completed" is a second, independent way a cycle counts as
  // settled here.
  const completedPlanCycleIds = new Set(
    [...planByCycle.entries()]
      .filter(([, plan]) => plan.status === "completed")
      .map(([cycleId]) => cycleId),
  );
  const isSettled = (cycle: DuesCycleOut) =>
    paidCycleIds.has(cycle.id) || completedPlanCycleIds.has(cycle.id);
  const outstanding = cycles.filter((cycle) => !isSettled(cycle));
  const settled = cycles.filter(isSettled);

  return (
    <Screen title="Dues" subtitle="Pay your chapter, not the app">
      <View style={{ gap: spacing.xl }}>
        {cyclesFailed ? (
          <EmptyState
            title="Couldn't load your dues"
            message="Check your connection and try again. This isn't a statement that you owe nothing."
          />
        ) : cycles.length === 0 ? (
          <EmptyState
            title="Nothing due"
            message="Your treasurer hasn't opened a dues cycle yet."
          />
        ) : null}

        {outstanding.length > 0 ? (
          <View>
            <SectionHeader title="Outstanding" caption="Due soonest first" />
            <View style={{ gap: spacing.md }}>
              {outstanding.map((cycle) => {
                const plan = planByCycle.get(cycle.id) ?? null;
                // An active plan replaces pay-now/outstanding entirely: the member
                // does not self-pay installments in this build (a treasurer records
                // each one), so there is no pay button here. A canceled plan is not
                // handled here — it falls through to the normal rendering below,
                // same as a member with no plan at all.
                if (plan !== null && plan.status === "active") {
                  return <PlanProgressCard key={cycle.id} cycleName={cycle.name} plan={plan} />;
                }
                return acceptsPayments ? (
                  <DuesPaymentScreen
                    key={cycle.id}
                    cycleId={cycle.id}
                    cycleName={cycle.name}
                    amountCents={cycle.amount_cents}
                    onPaid={() => void load(membership.chapter_id, membership.user_id)}
                  />
                ) : (
                  <Card key={cycle.id}>
                    <View style={{ gap: spacing.xs }}>
                      <AppText variant="headline">{cycle.name}</AppText>
                      <AppText variant="stat">{dollars(cycle.amount_cents)}</AppText>
                      <AppText variant="caption" tone="secondary">
                        Due {dueDate(cycle.due_date)}
                      </AppText>
                      <AppText variant="caption" tone="tertiary">
                        In-app payment isn't switched on yet. Your treasurer has to finish
                        Stripe setup. Pay them directly for now.
                      </AppText>
                    </View>
                  </Card>
                );
              })}
            </View>
          </View>
        ) : null}

        {settled.length > 0 ? (
          <View>
            <SectionHeader title="Settled" />
            <Card>
              <View style={{ gap: spacing.md }}>
                {settled.map((cycle) => {
                  // c235: a plan reaches "completed" on the backend's paid_at count
                  // (record_dues_installment_payment's remaining_unpaid), which a
                  // refund never reverses — so a cycle can sit in Settled with an
                  // installment whose money has gone back out. The section placement
                  // follows the server's status, but the badge must not say
                  // "complete" when the plan's own installments no longer agree.
                  const settledPlan = planByCycle.get(cycle.id) ?? null;
                  const planRefunded =
                    !paidCycleIds.has(cycle.id) &&
                    settledPlan !== null &&
                    settledPlan.installments.some((i) => i.paid_at !== null && !i.effective_paid);
                  return (
                    <View
                      key={cycle.id}
                      style={{
                        flexDirection: "row",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: spacing.sm,
                      }}
                    >
                      <View style={{ flex: 1 }}>
                        <AppText variant="bodyBold">{cycle.name}</AppText>
                        <AppText variant="caption" tone="secondary">
                          {dollars(cycle.amount_cents)}
                        </AppText>
                        {planRefunded ? (
                          <AppText variant="caption" tone="warning">
                            An installment on this plan was refunded. Check with your treasurer
                            before counting this cycle as done.
                          </AppText>
                        ) : null}
                      </View>
                      <Chip
                        // paidCycleIds takes precedence per the backend's own guard
                        // (create_dues_payment_intent / create_dues_payment_plan
                        // enforce a member never has both a ledger dues_payment AND
                        // a plan for the same cycle) — this is a label choice for
                        // the rare edge case, not a claim that both are expected.
                        label={
                          paidCycleIds.has(cycle.id)
                            ? "Paid"
                            : planRefunded
                              ? "Refunded"
                              : "Plan complete"
                        }
                        variant={planRefunded ? "warning" : "success"}
                      />
                    </View>
                  );
                })}
              </View>
            </Card>
          </View>
        ) : null}

        <AppText variant="caption" tone="tertiary" onPress={() => void Linking.openURL("https://stripe.com")}>
          Payments are processed by Stripe. Card details never reach Chirp.
        </AppText>
      </View>
    </Screen>
  );
}

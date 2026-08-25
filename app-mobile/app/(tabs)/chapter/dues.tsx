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
import { listDuesCycles, listLedger, type DuesCycleOut } from "@/api/finance";
import { getChapterPaymentsStatus } from "@/api/payments";
import { AppText, Card, Chip, EmptyState, Screen, SectionHeader } from "@/components";
import { calendarDay } from "@/lib/dates";
import DuesPaymentScreen from "@/payments/dues";
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
    setCycles(duesCycles ?? []);
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

  const outstanding = cycles.filter((cycle) => !paidCycleIds.has(cycle.id));
  const settled = cycles.filter((cycle) => paidCycleIds.has(cycle.id));

  return (
    <Screen title="Dues" subtitle="Pay your chapter, not the app">
      <View style={{ gap: spacing.xl }}>
        {cyclesFailed ? (
          <EmptyState
            title="Couldn't load your dues"
            message="Check your connection and try again — this isn't a statement that you owe nothing."
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
              {outstanding.map((cycle) =>
                acceptsPayments ? (
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
                        In-app payment isn't switched on yet — your treasurer has to finish
                        Stripe setup. Pay them directly for now.
                      </AppText>
                    </View>
                  </Card>
                ),
              )}
            </View>
          </View>
        ) : null}

        {settled.length > 0 ? (
          <View>
            <SectionHeader title="Settled" />
            <Card>
              <View style={{ gap: spacing.md }}>
                {settled.map((cycle) => (
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
                    </View>
                    <Chip label="Paid" variant="success" />
                  </View>
                ))}
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

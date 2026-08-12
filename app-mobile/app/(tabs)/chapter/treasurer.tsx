/**
 * Treasurer per DESIGN §7: HeroCard balance (big tabular stat on the gradient),
 * dues cycle progress caption, then the append-only ledger — +/- tabular
 * amounts in success/danger with Correction/Corrected chips.
 */

import { useEffect, useState } from "react";
import { View } from "react-native";

import {
  listDuesCycles,
  listLedger,
  type DuesCycleOut,
  type LedgerEntryOut,
} from "@/api/finance";
import {
  AppText,
  Card,
  Chip,
  EmptyState,
  HeroCard,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { MOCK_CURRENT_MEMBERSHIP } from "@/mocks/data";
import { spacing, typography } from "@/theme";

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

export default function TreasurerScreen() {
  const [cycles, setCycles] = useState<DuesCycleOut[]>([]);
  const [ledger, setLedger] = useState<LedgerEntryOut[] | null>(null);

  useEffect(() => {
    const load = async () => {
      const chapterId = MOCK_CURRENT_MEMBERSHIP.chapter_id;
      const [duesCycles, entries] = await Promise.all([
        listDuesCycles(chapterId),
        listLedger(chapterId),
      ]);
      setCycles(duesCycles);
      setLedger(entries);
    };
    void load();
  }, []);

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
          <SectionHeader
            title="Ledger"
            caption="Append-only — corrections are new offsetting entries"
          />
          <Card>
            {ledger !== null && sorted.length === 0 ? (
              <EmptyState emoji="🧾" title="No entries yet" message="Dues and expenses land here." />
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

/** Treasurer: dues cycle summary + append-only ledger list, corrections displayed explicitly. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import {
  listDuesCycles,
  listLedger,
  type DuesCycleOut,
  type LedgerEntryOut,
} from "@/api/finance";
import { AppText, Badge, Card, EmptyState, ListRow, Screen } from "@/components";
import { MOCK_CURRENT_MEMBERSHIP } from "@/mocks/data";
import { spacing } from "@/theme";

function dollars(cents: number): string {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function entryDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
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

  return (
    <Screen title="Treasurer" subtitle="Money lives in the dashboard, talk lives in chat">
      <View style={{ gap: spacing.md }}>
        {cycles.map((cycle) => {
          const paidCount = entries.filter(
            (entry) => entry.entry_type === "dues_payment" && entry.dues_cycle_id === cycle.id,
          ).length;
          return (
            <Card key={cycle.id}>
              <View style={{ gap: spacing.sm }}>
                <AppText variant="title">{cycle.name}</AppText>
                <AppText tone="secondary">
                  {dollars(cycle.amount_cents)} per member · due{" "}
                  {new Date(`${cycle.due_date}T00:00:00`).toLocaleDateString(undefined, {
                    month: "long",
                    day: "numeric",
                  })}
                </AppText>
                <AppText variant="caption" tone="success">
                  {paidCount} {paidCount === 1 ? "payment" : "payments"} collected
                </AppText>
              </View>
            </Card>
          );
        })}

        <Card>
          <View style={{ gap: spacing.sm }}>
            <AppText variant="title">Ledger</AppText>
            <AppText variant="caption" tone="secondary">
              Balance {dollars(balance)} · append-only — corrections are new offsetting entries
            </AppText>

            {ledger !== null && entries.length === 0 ? (
              <EmptyState title="No entries yet" />
            ) : (
              entries.map((entry, index) => (
                <ListRow
                  key={entry.id}
                  title={entry.description ?? entry.entry_type}
                  subtitle={`${entry.category ?? "uncategorized"} · ${entryDate(entry.created_at)}`}
                  divider={index < entries.length - 1}
                  right={
                    <View style={{ alignItems: "flex-end", gap: spacing.xs }}>
                      <AppText
                        tone={entry.amount_cents >= 0 ? "success" : "primary"}
                        style={{ fontWeight: "600" }}
                      >
                        {entry.amount_cents >= 0 ? "+" : ""}
                        {dollars(entry.amount_cents)}
                      </AppText>
                      {entry.entry_type === "correction" ? (
                        <Badge label="Correction" tone="accent" />
                      ) : correctedIds.has(entry.id) ? (
                        <Badge label="Corrected" tone="danger" />
                      ) : null}
                    </View>
                  }
                />
              ))
            )}
          </View>
        </Card>
      </View>
    </Screen>
  );
}

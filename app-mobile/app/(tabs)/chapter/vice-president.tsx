/**
 * Deputy President dashboard for the Vice President (board card c163).
 *
 * Jose's product ruling (board decisions log, Aug 24): the VP dashboard is DEPUTY
 * PRESIDENT — a READ view of president-admin data (roster, open invites, dues
 * status) with stand-in framing. Stand-in DELEGATION — acting on any of it — is
 * explicitly NOT in the alpha build, so this screen renders zero Buttons and fires
 * zero mutating calls. Every number here comes from GET /chapters/{id}/deputy-
 * overview, gated on the deputy_overview capability (backend/app/core/permissions.py)
 * rather than members_admin: that capability also gates PATCH endpoints the VP does
 * not hold today, and chapter_overview's own docstring explains why its payload
 * (which also carries attendance/lineage) is gated tighter than any single officer
 * capability. deputy_overview is read-only and does not exist in the app yet.
 *
 * Gated the same way president.tsx is (c136's lesson): a direct/deep-link nav for a
 * non-eligible member must land on an EmptyState, not a screen full of 403s, so this
 * checks roleMeta.capabilities itself instead of trusting the tile in chapter/
 * index.tsx alone.
 */

import { useEffect, useState } from "react";
import { View } from "react-native";
import { Feather } from "@expo/vector-icons";

import { getDeputyOverview, type DeputyOverview } from "@/api/chapters";
import { calendarDay } from "@/lib/dates";
import { ROLE_LABELS } from "@/lib/roleTerms";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import {
  AppText,
  Card,
  Chip,
  EmptyState,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { radii, spacing, typography, useAppearance, useTheme } from "@/theme";

/** Whole-dollar money for the summary tile — mirrors president.tsx's dollarsRounded
 * (same reasoning: cents are noise at a glance, the exact ledger is the treasurer's
 * screen either way). */
function dollarsRounded(cents: number): string {
  return (cents / 100).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

/** Due date as a calendar day — see @/lib/dates for why this cannot use the raw value. */
function dueDate(isoDay: string): string {
  return calendarDay(isoDay).toLocaleDateString(undefined, { month: "long", day: "numeric" });
}

/**
 * The stand-in framing this whole screen exists to carry: what the VP is looking at,
 * whose data it belongs to, and — explicitly — that nothing here is actionable from
 * this screen. Same "stated, not offered" info-row idiom as OrgsScreen's
 * ActivesOnlyHiddenNotice, not a shared component (that one is chapter/index.tsx-local
 * too).
 */
function StandInNotice() {
  const palette = useTheme();
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "flex-start",
        gap: spacing.md,
        padding: spacing.md,
        borderRadius: radii.input,
        backgroundColor: palette.surfaceAlt,
        borderWidth: 1,
        borderColor: palette.border,
      }}
    >
      <Feather name="eye" size={18} color={palette.inkFaint} style={{ marginTop: 2 }} />
      <View style={{ flex: 1, gap: 2 }}>
        <AppText variant="bodyBold">Standing in for the president</AppText>
        <AppText variant="caption" tone="secondary">
          This is what the president sees: roster, open invites, and dues status, so
          you can cover for them. Nothing on this screen can be changed; edits still
          happen on the president's own dashboard.
        </AppText>
      </View>
    </View>
  );
}

export default function VicePresidentScreen() {
  const palette = useTheme();
  const { campusColors } = useAppearance();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;

  const [overview, setOverview] = useState<DeputyOverview | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    if (chapterId === null) {
      setOverview(null);
      setLoadFailed(false);
      return;
    }
    setLoadFailed(false);
    getDeputyOverview(chapterId)
      .then(setOverview)
      .catch(() => {
        setOverview(null);
        setLoadFailed(true);
      });
  }, [chapterId]);

  const loading =
    sessionStatus === "loading" ||
    (membership !== null && chapterLoading) ||
    (overview === null && !loadFailed);

  const tile = {
    flex: 1,
    backgroundColor: palette.surface,
    borderRadius: radii.card,
    padding: spacing.lg,
    gap: spacing.xs,
  } as const;

  // Same posture as president.tsx (board c136): roleMeta is null while loading OR on
  // a failed fetch, and this defaults to "not eligible" rather than ever flashing the
  // real dashboard before eligibility is confirmed.
  const isDeputy = roleMeta?.capabilities.includes("deputy_overview") ?? false;
  if (!isDeputy) {
    return (
      <Screen title="Deputy President" subtitle="A stand-in view of the president's dashboard">
        <EmptyState
          title="Vice president/president only"
          message="This dashboard is limited to your chapter's vice president or president."
        />
      </Screen>
    );
  }

  return (
    <Screen title="Deputy President" subtitle="A stand-in view of the president's dashboard">
      {loading ? (
        <EmptyState title="Loading deputy dashboard..." />
      ) : loadFailed || overview === null ? (
        <EmptyState
          title="Couldn't load the deputy dashboard"
          message="Check your connection and try again."
        />
      ) : (
        <View style={{ gap: spacing.xl }}>
          <StandInNotice />

          <View>
            <SectionHeader
              title="Roster"
              caption={`${overview.roster.active} active member${overview.roster.active === 1 ? "" : "s"}`}
            />
            <View style={{ flexDirection: "row", gap: spacing.md }}>
              <View style={tile}>
                <AppText variant="caption" tone="tertiary">
                  ACTIVE
                </AppText>
                <AppText style={{ ...typography.stat, color: palette.ink }}>
                  {overview.roster.active}
                </AppText>
                <AppText variant="caption" tone="secondary">
                  {overview.roster.inactive === 0 ? "none inactive" : `${overview.roster.inactive} inactive`}
                </AppText>
              </View>
              <View style={tile}>
                <AppText variant="caption" tone="tertiary">
                  OPEN INVITES
                </AppText>
                <AppText style={{ ...typography.stat, color: palette.ink }}>
                  {overview.invites.live_codes}
                </AppText>
                <AppText variant="caption" tone="secondary">
                  {overview.invites.live_codes === 0
                    ? "none live"
                    : `${overview.invites.remaining_uses} more could join`}
                </AppText>
              </View>
            </View>
            {overview.roster.by_role.length > 0 ? (
              <Card style={{ marginTop: spacing.md }}>
                {overview.roster.by_role.map((entry, index) => (
                  <ListRow
                    key={entry.role}
                    title={ROLE_LABELS[entry.role] ?? entry.role}
                    right={<Chip label={String(entry.count)} variant="neutral" />}
                    divider={index < overview.roster.by_role.length - 1}
                  />
                ))}
              </Card>
            ) : null}
          </View>

          <View>
            <SectionHeader
              title="Dues"
              caption={overview.dues.cycle_name ?? "No cycle opened yet"}
            />
            <Card>
              {overview.dues.cycle_id === null ? (
                <AppText variant="body" tone="secondary">
                  No dues cycle has been opened yet. Your treasurer starts one from the
                  Dues screen.
                </AppText>
              ) : (
                <View style={{ gap: spacing.sm }}>
                  {/* Rule 6: money in the stat face, tabular — same gold moment
                      president.tsx uses for the identical figure. */}
                  <AppText
                    style={{
                      ...typography.stat,
                      fontSize: 28,
                      lineHeight: 34,
                      color: campusColors.secondary,
                    }}
                  >
                    {dollarsRounded(overview.dues.collected_cents)}
                  </AppText>
                  <AppText variant="caption" tone="secondary">
                    {overview.dues.paid_members} of {overview.roster.active} paid
                    {overview.dues.due_date === null ? "" : ` · due ${dueDate(overview.dues.due_date)}`}
                  </AppText>
                  {overview.dues.outstanding_members > 0 ? (
                    <Chip
                      label={`${overview.dues.outstanding_members} still owe dues`}
                      variant="warning"
                    />
                  ) : null}
                </View>
              )}
            </Card>
          </View>
        </View>
      )}
    </Screen>
  );
}

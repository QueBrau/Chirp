/**
 * President: role/status/pledge_class editing per member, plus chapter identity
 * (org_name/chapter_name) — board card c77.
 *
 * Both PATCH endpoints this screen calls already existed and were unused:
 * updateMember() and updateChapter() had zero call sites anywhere in the app.
 * PATCH /chapters/{id}/members is gated on the members_admin capability (c80);
 * this screen is the first thing that consumes it, so role/status editing is
 * driven off role-meta's `roles` list rather than a second hardcoded taxonomy.
 *
 * THE TRAP THIS SCREEN IS DESIGNED AROUND: create_chapter is platform-admin-only
 * and the creator auto-becomes the one president; the only other way to mint one
 * is a president inviting someone NOT yet in the chapter at the president role.
 * So the moment a chapter's last active president stops being president — leaves,
 * is demoted, whatever — nobody in that chapter can ever change a role again, and
 * recovery is a manual DB edit (c83 owns whether that ever gets a real fix; this
 * screen only owns not making it easy to hit by accident).
 *
 * confirmRoleChange refuses outright, rather than confirming harder, any edit
 * that would leave the chapter with zero active presidents. It is a client-side
 * guard, not the source of truth — the server enforces nothing about president
 * count, on purpose, since c77 only asked for the screen and enforcing a new
 * server invariant here would be c83's scope, not this card's.
 */

import { useCallback, useEffect, useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import {
  getChapter,
  getChapterOverview,
  listMembers,
  updateChapter,
  updateMember,
  type ChapterOut,
  type ChapterOverview,
  type MemberOut,
  type MembershipStatus,
  type RoleName,
} from "@/api/chapters";
import { confirmAction, showAlert, showApiError } from "@/lib/alert";
import { calendarDay } from "@/lib/dates";
import { chipVariant, roleLabel } from "@/lib/roleTerms";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { currentSemesterWindow } from "@/org/semester";
import {
  AppText,
  Button,
  Card,
  Chip,
  EmptyState,
  GradientAvatar,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { radii, spacing, typography, useAppearance, useTheme } from "@/theme";

const STATUS_LABELS: Record<MembershipStatus, string> = {
  active: "Active",
  inactive: "Inactive",
  removed: "Removed",
};

/**
 * Whole-dollar money for the summary tiles.
 *
 * The ledger shows exact cents and treasurer.tsx is the authority for them; at a
 * glance the cents are noise that pushes the figure wider than the tile it sits in,
 * and the exact number is one screen away either way. Same reasoning as
 * treasurer.tsx's dollarsRounded, which this deliberately mirrors rather than
 * inventing a third money format.
 */
function dollarsRounded(cents: number): string {
  return (cents / 100).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

/** Due date as a calendar day. See @/lib/dates for why this cannot use the raw value. */
function dueDate(isoDay: string): string {
  return calendarDay(isoDay).toLocaleDateString(undefined, {
    month: "long",
    day: "numeric",
  });
}

function shortUserId(userId: string): string {
  return userId.length > 12 ? `${userId.slice(0, 6)}…${userId.slice(-4)}` : userId;
}

/**
 * The at-a-glance half of the President dashboard (board card c171).
 *
 * DESIGN.md rule 1 (zones, not card soup) drives the shape: one prominent money card,
 * a two-up row of compact counts, then a quiet attention list that only renders rows
 * that need action. An even stack of identical cards is the slop tell the rule names.
 *
 * The gold moment (rule 4, "never zero gold either") is the collected figure — the
 * chapter's analogue of the treasurer balance the design notes already single out.
 * Outstanding uses `warning`, not gold: it is a pending state, not a delight.
 *
 * Everything here is READ-ONLY on purpose. Each number belongs to an officer screen
 * that owns the actions on it, and a president who wants to act is one tap from the
 * Tools grid. Duplicating the controls here would mean two places to keep correct.
 */
function OverviewPanel({
  overview,
  accent,
}: {
  overview: ChapterOverview;
  accent: string;
}) {
  const palette = useTheme();
  const { roster, dues, attendance, lineage, invites } = overview;

  // Only rows that need action are rendered. A list padded out with "0 pairs waiting"
  // is card soup with extra steps (DESIGN.md rule 1), and a president scanning for what
  // to do should not have to read past the things that are already fine.
  //
  // No status badge on these rows: the label IS the status, and an amber "Open" chip
  // beside it competed with the collected figure for the screen's one gold moment
  // (rule 4) while saying nothing the sentence had not already said.
  const attention: { key: string; label: string; detail: string }[] = [];
  if (dues.outstanding_members > 0) {
    attention.push({
      key: "dues",
      label: `${dues.outstanding_members} still owe dues`,
      detail: dues.cycle_name ?? "current cycle",
    });
  }
  if (attendance.members_with_absence > 0) {
    attention.push({
      key: "attendance",
      label: `${attendance.members_with_absence} missed a meeting`,
      detail: `${attendance.meetings_in_window} this semester`,
    });
  }
  if (lineage.unconfirmed_edges > 0) {
    attention.push({
      key: "lineage",
      label: `${lineage.unconfirmed_edges} big/little ${
        lineage.unconfirmed_edges === 1 ? "pair" : "pairs"
      } unconfirmed`,
      detail: "waiting on the little",
    });
  }
  if (invites.live_codes > 0) {
    attention.push({
      key: "invites",
      label: `${invites.live_codes} invite ${invites.live_codes === 1 ? "code" : "codes"} live`,
      detail: `${invites.remaining_uses} more could join`,
    });
  }

  const tile = {
    flex: 1,
    backgroundColor: palette.surface,
    borderRadius: radii.card,
    padding: spacing.lg,
    gap: spacing.xs,
  } as const;

  return (
    <View style={{ gap: spacing.lg }}>
      <SectionHeader
        title="This chapter, right now"
        caption={`Updated ${new Date(overview.generated_at).toLocaleTimeString(undefined, {
          hour: "numeric",
          minute: "2-digit",
        })}`}
      />

      <Card>
        {dues.cycle_id === null ? (
          <View style={{ gap: spacing.xs }}>
            <AppText variant="caption" tone="tertiary">
              DUES
            </AppText>
            <AppText variant="body" tone="secondary">
              No dues cycle has been opened yet. Your treasurer starts one from the Dues
              screen.
            </AppText>
          </View>
        ) : (
          <View style={{ gap: spacing.sm }}>
            <AppText variant="caption" tone="tertiary">
              {dues.cycle_name?.toUpperCase()}
            </AppText>
            {/* Rule 6: money in the stat face, tabular, and this is the screen's gold. */}
            <AppText style={{ ...typography.stat, fontSize: 28, lineHeight: 34, color: accent }}>
              {dollarsRounded(dues.collected_cents)}
            </AppText>
            <AppText variant="caption" tone="secondary">
              {dues.paid_members} of {roster.active} paid
              {dues.due_date === null ? "" : ` \u00b7 due ${dueDate(dues.due_date)}`}
            </AppText>
          </View>
        )}
      </Card>

      <View style={{ flexDirection: "row", gap: spacing.md }}>
        <View style={tile}>
          <AppText variant="caption" tone="tertiary">
            ROSTER
          </AppText>
          <AppText style={{ ...typography.stat, color: palette.textPrimary }}>
            {roster.active}
          </AppText>
          <AppText variant="caption" tone="secondary">
            {roster.inactive === 0 ? "all active" : `${roster.inactive} inactive`}
          </AppText>
        </View>
        <View style={tile}>
          <AppText variant="caption" tone="tertiary">
            MEETINGS
          </AppText>
          <AppText style={{ ...typography.stat, color: palette.textPrimary }}>
            {attendance.meetings_in_window}
          </AppText>
          <AppText variant="caption" tone="secondary">
            this semester
          </AppText>
        </View>
      </View>

      {attention.length === 0 ? (
        <AppText variant="caption" tone="secondary">
          Nothing needs your attention right now.
        </AppText>
      ) : (
        <Card>
          {attention.map((item, index) => (
            <ListRow
              key={item.key}
              title={item.label}
              subtitle={item.detail}
              divider={index < attention.length - 1}
            />
          ))}
        </Card>
      )}
    </View>
  );
}

export default function PresidentScreen() {
  const palette = useTheme();
  // Spartan gold, the same token the Orgs header bar uses (DESIGN.md 8.5/10.4).
  // NOT palette.warning: warning is the pending-state orange, and dues collected is
  // the screen's delight number, not a caution.
  const { campusColors } = useAppearance();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [chapter, setChapter] = useState<ChapterOut | null>(null);
  // null while loading OR after a failed fetch. The overview is a summary of screens
  // that all still work on their own, so a failure here hides the panel rather than
  // taking down the roster editing this screen exists for.
  const [overview, setOverview] = useState<ChapterOverview | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);

  const [orgName, setOrgName] = useState("");
  const [chapterName, setChapterName] = useState("");
  const [savingIdentity, setSavingIdentity] = useState(false);
  // Controlled per-member, keyed on membership id, so onBlur can read the typed
  // value directly instead of relying on a native event field. Seeded lazily
  // per row below rather than from `members` up front, since members loads async.
  const [pledgeDrafts, setPledgeDrafts] = useState<Record<string, string>>({});

  const refreshMembers = useCallback(async () => {
    if (chapterId === null) return;
    try {
      setMembers(await listMembers(chapterId));
    } catch {
      setMembers([]);
    }
  }, [chapterId]);

  useEffect(() => {
    void refreshMembers();
  }, [refreshMembers]);

  useEffect(() => {
    if (chapterId === null) {
      setOverview(null);
      return;
    }
    // Same window the Secretary dashboard computes, from the one shared helper, so the
    // meeting counts on the two screens cannot disagree by a boundary meeting.
    getChapterOverview(chapterId, currentSemesterWindow(new Date()))
      .then(setOverview)
      .catch(() => setOverview(null));
  }, [chapterId]);

  useEffect(() => {
    if (chapterId === null) {
      setChapter(null);
      return;
    }
    getChapter(chapterId)
      .then((value) => {
        setChapter(value);
        setOrgName(value.org_name);
        setChapterName(value.chapter_name ?? "");
      })
      .catch(() => setChapter(null));
  }, [chapterId]);

  const loading =
    sessionStatus === "loading" || (membership !== null && chapterLoading) || members === null;

  const active = (members ?? []).filter((m) => m.status !== "removed");
  const activePresidentCount = active.filter(
    (m) => m.role === "president" && m.status === "active",
  ).length;

  /**
   * Would this change leave zero active presidents? Evaluated against the CURRENT
   * roster plus the pending edit — not just "is the target currently president" —
   * so demoting the second-to-last active president is caught the same as
   * demoting the only one.
   */
  const wouldOrphanChapter = (target: MemberOut, nextRole: RoleName, nextStatus: MembershipStatus) => {
    const targetIsActivePresident = target.role === "president" && target.status === "active";
    const staysActivePresident = nextRole === "president" && nextStatus === "active";
    if (!targetIsActivePresident || staysActivePresident) return false;
    return activePresidentCount <= 1;
  };

  const applyMemberChange = async (
    target: MemberOut,
    changes: { role?: RoleName; status?: MembershipStatus; pledge_class?: string | null },
  ) => {
    if (chapterId === null) return;
    const nextRole = changes.role ?? target.role;
    const nextStatus = changes.status ?? target.status;

    if (wouldOrphanChapter(target, nextRole, nextStatus)) {
      showAlert(
        "This chapter would lose its last president",
        "At least one active president has to remain, or nobody will be able to change a " +
          "role here again — recovery would need to go outside the app. Promote someone " +
          "else to president first.",
      );
      return;
    }

    setSavingId(target.id);
    try {
      await updateMember(chapterId, { user_id: target.user_id, ...changes });
      await refreshMembers();
      setExpandedId(null);
    } catch (error) {
      showApiError(error, "Couldn't update that member");
    } finally {
      setSavingId(null);
    }
  };

  const confirmRoleChange = (target: MemberOut, nextRole: RoleName) => {
    if (nextRole === target.role) return;
    // The person granting a role can be the person losing it — always confirm,
    // never apply a role change on tap alone.
    confirmAction({
      title: `Change ${target.display_name || "this member"} to ${roleLabel(nextRole)}?`,
      message:
        target.role === "president"
          ? "They're currently President. This takes that role away from them."
          : undefined,
      confirmLabel: "Confirm",
      onConfirm: () => void applyMemberChange(target, { role: nextRole }),
    });
  };

  const confirmStatusChange = (target: MemberOut, nextStatus: MembershipStatus) => {
    if (nextStatus === target.status) return;
    confirmAction({
      title: `Mark ${target.display_name || "this member"} ${STATUS_LABELS[nextStatus].toLowerCase()}?`,
      message:
        nextStatus !== "active" ? "They'll drop out of the active roster and role-gated tools." : undefined,
      confirmLabel: "Confirm",
      destructive: nextStatus === "removed",
      onConfirm: () => void applyMemberChange(target, { status: nextStatus }),
    });
  };

  const savePledgeClass = async (target: MemberOut) => {
    const value = pledgeDrafts[target.id] ?? target.pledge_class ?? "";
    const trimmed = value.trim();
    if (trimmed === (target.pledge_class ?? "")) return;
    await applyMemberChange(target, { pledge_class: trimmed.length > 0 ? trimmed : null });
  };

  const identityChanged =
    chapter !== null &&
    (orgName.trim() !== chapter.org_name || chapterName.trim() !== (chapter.chapter_name ?? ""));

  const saveIdentity = async () => {
    if (chapterId === null || !identityChanged || orgName.trim().length === 0) return;
    setSavingIdentity(true);
    try {
      // The server treats a null field as "leave unchanged", never "clear it"
      // (same convention update_member already uses for pledge_class) — so an
      // empty chapter-name box must omit the field, not send null. Sending null
      // here would silently no-op a clear the president thinks just happened,
      // leaving the input showing blank while the server still has the old
      // value. Found live: this route didn't exist at all until this same
      // change added it (see the router docstring) and the client had been
      // built assuming it worked.
      const updated = await updateChapter(chapterId, {
        org_name: orgName.trim(),
        ...(chapterName.trim().length > 0 ? { chapter_name: chapterName.trim() } : {}),
      });
      setChapter(updated);
      setOrgName(updated.org_name);
      setChapterName(updated.chapter_name ?? "");
    } catch (error) {
      showApiError(error, "Couldn't update chapter details");
    } finally {
      setSavingIdentity(false);
    }
  };

  const inputStyle = {
    ...typography.body,
    color: palette.textPrimary,
    backgroundColor: palette.surfaceAlt,
    borderRadius: radii.input,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  };

  const knownRoles = roleMeta?.roles ?? [];
  const eboardRoles = roleMeta?.eboard ?? [];

  // Board card c136 (security's consistency finding): this screen was reachable by
  // direct/deep-link navigation for any chapter member — only the TILE in
  // chapter/index.tsx's OrgToolsSegment was gated. Both server actions (update_member,
  // update_chapter) already required MEMBERS_ADMIN (president-only), so this never
  // leaked data or capability, only rendered controls that would 403 on use. Matches
  // moderation.tsx's exact pattern: a direct nav re-checks the role itself instead of
  // trusting the tile alone, and lands on an EmptyState rather than a screen full of
  // would-be 403s. roleMeta is null while loading OR on a failed fetch — same accepted
  // ambiguity as moderation.tsx's isEboard check, defaulting to "not eligible" rather
  // than ever showing the real president's tools before eligibility is confirmed.
  const isPresident = roleMeta?.capabilities.includes("members_admin") ?? false;
  if (!isPresident) {
    return (
      <Screen title="President" subtitle="Roles, status, and chapter details">
        <EmptyState
          title="President only"
          message="This dashboard is limited to your chapter's president."
        />
      </Screen>
    );
  }

  return (
    <Screen title="President" subtitle="Roles, status, and chapter details">
      {loading ? (
        <EmptyState title="Loading..." />
      ) : (
        <View style={{ gap: spacing.xl }}>
          {overview === null ? null : (
            <OverviewPanel overview={overview} accent={campusColors.secondary} />
          )}

          <View>
            <SectionHeader title="Chapter details" caption="Org and chapter name" />
            <Card>
              <View style={{ gap: spacing.md }}>
                <View style={{ gap: spacing.xs }}>
                  <AppText variant="caption" tone="tertiary">
                    ORG NAME
                  </AppText>
                  <TextInput value={orgName} onChangeText={setOrgName} style={inputStyle} />
                </View>
                <View style={{ gap: spacing.xs }}>
                  <AppText variant="caption" tone="tertiary">
                    CHAPTER NAME (OPTIONAL)
                  </AppText>
                  <TextInput
                    value={chapterName}
                    onChangeText={setChapterName}
                    placeholder="e.g. Alpha"
                    placeholderTextColor={palette.textTertiary}
                    style={inputStyle}
                  />
                </View>
                <Button
                  label={savingIdentity ? "Saving…" : "Save details"}
                  onPress={() => void saveIdentity()}
                  disabled={!identityChanged || orgName.trim().length === 0 || savingIdentity}
                />
              </View>
            </Card>
          </View>

          <View>
            <SectionHeader
              title="Members"
              caption={`${active.length} on the roster · tap someone to edit`}
            />
            {active.length === 0 ? (
              <EmptyState title="No members" message="Invites you've sent will show up here once redeemed." />
            ) : (
              <Card>
                {active.map((member, index) => {
                  const label =
                    member.display_name.length > 0 ? member.display_name : shortUserId(member.user_id);
                  const expanded = expandedId === member.id;
                  const saving = savingId === member.id;
                  return (
                    <View key={member.id}>
                      <ListRow
                        title={label}
                        subtitle={member.pledge_class ?? undefined}
                        left={<GradientAvatar name={label} size={40} photoUrl={member.avatar_url} />}
                        right={
                          <Chip
                            label={roleLabel(member.role)}
                            variant={chipVariant(member.role, eboardRoles)}
                          />
                        }
                        onPress={() => setExpandedId(expanded ? null : member.id)}
                        divider={index < active.length - 1 && !expanded}
                      />
                      {expanded ? (
                        <View
                          style={{
                            paddingHorizontal: spacing.md,
                            paddingBottom: spacing.md,
                            gap: spacing.md,
                          }}
                        >
                          <View style={{ gap: spacing.xs }}>
                            <AppText variant="caption" tone="tertiary">
                              ROLE
                            </AppText>
                            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
                              {(knownRoles.length > 0 ? knownRoles : [member.role]).map((role) => (
                                <Pressable
                                  key={role}
                                  accessibilityRole="button"
                                  accessibilityState={{ selected: member.role === role }}
                                  disabled={saving}
                                  onPress={() => confirmRoleChange(member, role)}
                                  style={({ pressed }) => ({ opacity: pressed || saving ? 0.6 : 1 })}
                                >
                                  <Chip
                                    label={roleLabel(role)}
                                    variant={member.role === role ? "accent" : "neutral"}
                                  />
                                </Pressable>
                              ))}
                            </View>
                          </View>

                          <View style={{ gap: spacing.xs }}>
                            <AppText variant="caption" tone="tertiary">
                              STATUS
                            </AppText>
                            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
                              {(["active", "inactive", "removed"] as const).map((status) => (
                                <Pressable
                                  key={status}
                                  accessibilityRole="button"
                                  accessibilityState={{ selected: member.status === status }}
                                  disabled={saving}
                                  onPress={() => confirmStatusChange(member, status)}
                                  style={({ pressed }) => ({ opacity: pressed || saving ? 0.6 : 1 })}
                                >
                                  <Chip
                                    label={STATUS_LABELS[status]}
                                    variant={
                                      member.status === status
                                        ? status === "removed"
                                          ? "danger"
                                          : "accent"
                                        : "neutral"
                                    }
                                  />
                                </Pressable>
                              ))}
                            </View>
                          </View>

                          <View style={{ gap: spacing.xs }}>
                            <AppText variant="caption" tone="tertiary">
                              PLEDGE CLASS
                            </AppText>
                            <TextInput
                              value={pledgeDrafts[member.id] ?? member.pledge_class ?? ""}
                              onChangeText={(text) =>
                                setPledgeDrafts((current) => ({ ...current, [member.id]: text }))
                              }
                              placeholder="e.g. Fall 2026"
                              placeholderTextColor={palette.textTertiary}
                              editable={!saving}
                              // onEndEditing is a native-only RN event — react-native-web's
                              // TextInput does not implement it at all (checked against
                              // node_modules directly, not assumed), so a save-on-leave
                              // handler wired to it silently never fires on web. onBlur is
                              // implemented on both platforms; its event carries no text
                              // field (that's onEndEditing-specific), so the field is
                              // controlled per-row and onBlur just reads current state.
                              onBlur={() => void savePledgeClass(member)}
                              style={inputStyle}
                            />
                          </View>
                        </View>
                      ) : null}
                    </View>
                  );
                })}
              </Card>
            )}
          </View>
        </View>
      )}
    </Screen>
  );
}

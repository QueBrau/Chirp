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
import { Alert, Pressable, TextInput, View } from "react-native";

import {
  getChapter,
  listMembers,
  updateChapter,
  updateMember,
  type ChapterOut,
  type MemberOut,
  type MembershipStatus,
  type RoleName,
} from "@/api/chapters";
import { ApiError } from "@/api/client";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import {
  AppText,
  Button,
  Card,
  Chip,
  type ChipVariant,
  EmptyState,
  GradientAvatar,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { radii, spacing, typography, useTheme } from "@/theme";

const ROLE_LABELS: Record<RoleName, string> = {
  president: "President",
  vice_president: "Vice President",
  treasurer: "Treasurer",
  secretary: "Secretary",
  historian: "Historian",
  member: "Member",
  pledge: "Pledge",
  alumni: "Alum",
};

const STATUS_LABELS: Record<MembershipStatus, string> = {
  active: "Active",
  inactive: "Inactive",
  removed: "Removed",
};

function prettifyRole(role: string): string {
  return role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function chipVariant(role: RoleName, eboard: RoleName[]): ChipVariant {
  if (eboard.includes(role)) return "accent";
  return role === "pledge" ? "warning" : "neutral";
}

function shortUserId(userId: string): string {
  return userId.length > 12 ? `${userId.slice(0, 6)}…${userId.slice(-4)}` : userId;
}

/** ApiError carries a server-provided `.detail`; anything else gets a generic fallback. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  Alert.alert(title, message);
}

export default function PresidentScreen() {
  const palette = useTheme();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [chapter, setChapter] = useState<ChapterOut | null>(null);
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
      Alert.alert(
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
    Alert.alert(
      `Change ${target.display_name || "this member"} to ${ROLE_LABELS[nextRole] ?? prettifyRole(nextRole)}?`,
      target.role === "president"
        ? "They're currently President. This takes that role away from them."
        : undefined,
      [
        { text: "Cancel", style: "cancel" },
        { text: "Confirm", onPress: () => void applyMemberChange(target, { role: nextRole }) },
      ],
    );
  };

  const confirmStatusChange = (target: MemberOut, nextStatus: MembershipStatus) => {
    if (nextStatus === target.status) return;
    Alert.alert(
      `Mark ${target.display_name || "this member"} ${STATUS_LABELS[nextStatus].toLowerCase()}?`,
      nextStatus !== "active" ? "They'll drop out of the active roster and role-gated tools." : undefined,
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Confirm",
          style: nextStatus === "removed" ? "destructive" : "default",
          onPress: () => void applyMemberChange(target, { status: nextStatus }),
        },
      ],
    );
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

  return (
    <Screen title="President" subtitle="Roles, status, and chapter details">
      {loading ? (
        <EmptyState title="Loading..." />
      ) : (
        <View style={{ gap: spacing.xl }}>
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
                            label={ROLE_LABELS[member.role] ?? prettifyRole(member.role)}
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
                                    label={ROLE_LABELS[role] ?? prettifyRole(role)}
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

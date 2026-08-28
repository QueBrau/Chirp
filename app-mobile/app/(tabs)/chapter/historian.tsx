/**
 * Historian (c79): the write surface for the family tree — families, pairing,
 * and what is still owed. SPEC §7 m6 names the historian for the job; edit
 * rights are "Historian/e-board", so the gate is the lineage_admin capability
 * (EBOARD-wide) from role-meta, matching president.tsx's c136 direct-nav guard.
 *
 * The pairing list here is the canvas flow's LIST FALLBACK — it has to work in
 * the state every chapter starts in: members exist, zero edges, so the canvas
 * draws nothing and there is no node to tap. Unplaced members lead the section
 * for the same reason.
 *
 * The archive half of the historian's real job (composites, event photos,
 * chapter history) is deliberately NOT here: c70's media pipeline landed
 * server-side, but it still owes an EAS dev build. See card c79 before adding
 * a photos tile.
 */

import { Feather } from "@expo/vector-icons";
import { useRouter } from "expo-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, View } from "react-native";

import { listMembers, type MemberOut } from "@/api/chapters";
import { getLineage, type LineageEdgeOut, type LineageTreeOut } from "@/api/lineage";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import {
  AppText,
  Button,
  Card,
  Chip,
  EmptyState,
  GradientAvatar,
  Screen,
  SectionHeader,
} from "@/components";
import { showAlert } from "@/lib/alert";
import { FamilySheet } from "@/tree/FamilySheet";
import { PairSheet } from "@/tree/PairSheet";
import { radii, spacing, useTheme } from "@/theme";

export default function HistorianScreen() {
  const palette = useTheme();
  const router = useRouter();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;

  const [tree, setTree] = useState<LineageTreeOut | null>(null);
  const [treeLoading, setTreeLoading] = useState(true);
  const [members, setMembers] = useState<MemberOut[]>([]);
  const [familySheetOpen, setFamilySheetOpen] = useState(false);
  const [pairSheetOpen, setPairSheetOpen] = useState(false);
  const [pairLittleId, setPairLittleId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (chapterId === null) {
      setTree(null);
      setTreeLoading(false);
      return;
    }
    setTreeLoading(true);
    try {
      setTree(await getLineage(chapterId));
    } catch {
      setTree(null);
    } finally {
      setTreeLoading(false);
    }
  }, [chapterId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (chapterId === null) return;
    listMembers(chapterId)
      .then(setMembers)
      .catch(() => setMembers([]));
  }, [chapterId]);

  const nameOf = useCallback(
    (userId: string): string =>
      members.find((member) => member.user_id === userId)?.display_name ??
      tree?.nodes.find((node) => node.user_id === userId)?.display_name ??
      "Unknown",
    [members, tree],
  );

  const placedIds = useMemo(() => {
    const ids = new Set<string>();
    for (const edge of tree?.edges ?? []) {
      ids.add(edge.big_user_id);
      ids.add(edge.little_user_id);
    }
    return ids;
  }, [tree]);

  const unplaced = useMemo(
    () =>
      members
        .filter((member) => member.status === "active" && !placedIds.has(member.user_id))
        .sort((a, b) => a.display_name.localeCompare(b.display_name)),
    [members, placedIds],
  );

  const pending = useMemo(
    () => (tree?.edges ?? []).filter((edge) => !edge.confirmed_by_little),
    [tree],
  );

  const familySize = useCallback(
    (familyId: string): number =>
      (tree?.nodes ?? []).filter((node) => node.family_id === familyId).length,
    [tree],
  );

  const openPairFor = (littleId: string | null) => {
    setPairLittleId(littleId);
    setPairSheetOpen(true);
  };

  const onEdgeSaved = (edge: LineageEdgeOut) => {
    showAlert(
      "Paired",
      `${nameOf(edge.little_user_id)} will be asked to confirm ${nameOf(edge.big_user_id)} as their big.`,
    );
    void reload();
  };

  // c136 pattern: the tile gate alone is not a gate — a direct/deep-link nav
  // re-checks the capability itself. roleMeta null (loading or failed) fails
  // CLOSED, same accepted ambiguity as president.tsx.
  const canEdit = roleMeta?.capabilities.includes("lineage_admin") ?? false;
  if (!canEdit) {
    return (
      <Screen title="Historian" subtitle="Families, bigs, and littles">
        <EmptyState
          title="E-board only"
          message="Recording families and pairings is limited to your chapter's e-board."
        />
      </Screen>
    );
  }

  const loading = sessionStatus === "loading" || (membership !== null && chapterLoading) || treeLoading;

  return (
    <Screen title="Historian" subtitle="Families, bigs, and littles">
      {loading || chapterId === null ? (
        <EmptyState title="Loading lineage..." />
      ) : (
        <View style={{ gap: spacing.xl }}>
          <View>
            <SectionHeader
              title="Families"
              caption="Each renders in its own color on the tree"
              right={
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="New family"
                  onPress={() => setFamilySheetOpen(true)}
                  hitSlop={spacing.sm}
                  style={({ pressed }) => ({
                    width: 32,
                    height: 32,
                    borderRadius: radii.pill,
                    backgroundColor: palette.accentSoft,
                    alignItems: "center",
                    justifyContent: "center",
                    opacity: pressed ? 0.8 : 1,
                  })}
                >
                  <Feather name="plus" size={16} color={palette.accent} />
                </Pressable>
              }
            />
            {tree === null || tree.families.length === 0 ? (
              <EmptyState
                title="No families yet"
                message="Start one. Every big/little pairing can live in a family, and the tree colors itself by them."
                actionLabel="New family"
                onAction={() => setFamilySheetOpen(true)}
              />
            ) : (
              <Card>
                <View style={{ gap: spacing.md }}>
                  {tree.families.map((family) => (
                    <View
                      key={family.id}
                      style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}
                    >
                      <View
                        style={{
                          width: spacing.lg,
                          height: spacing.lg,
                          borderRadius: radii.pill,
                          backgroundColor: family.color,
                        }}
                      />
                      <AppText variant="bodyBold" style={{ flex: 1 }}>
                        {family.name}
                      </AppText>
                      <AppText variant="caption" tone="secondary">
                        {familySize(family.id)} on the tree
                      </AppText>
                    </View>
                  ))}
                </View>
              </Card>
            )}
          </View>

          <View>
            <SectionHeader
              title="Bigs and littles"
              caption={
                unplaced.length > 0
                  ? `${unplaced.length} ${unplaced.length === 1 ? "member has" : "members have"} no place on the tree yet`
                  : "Everyone active is on the tree"
              }
            />
            <View style={{ gap: spacing.md }}>
              {unplaced.length > 0 ? (
                <Card>
                  <View style={{ gap: spacing.sm }}>
                    {unplaced.map((member) => (
                      <View
                        key={member.user_id}
                        style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}
                      >
                        <GradientAvatar
                          name={member.display_name}
                          size={32}
                          photoUrl={member.avatar_url}
                        />
                        <AppText variant="body" style={{ flex: 1 }}>
                          {member.display_name}
                        </AppText>
                        <Pressable
                          accessibilityRole="button"
                          accessibilityLabel={`Pair ${member.display_name}`}
                          onPress={() => openPairFor(member.user_id)}
                          style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
                        >
                          <Chip label="Pair" variant="accent" />
                        </Pressable>
                      </View>
                    ))}
                  </View>
                </Card>
              ) : null}
              <Button
                label="Pair a big and little"
                variant="secondary"
                onPress={() => openPairFor(null)}
              />
            </View>
          </View>

          {pending.length > 0 ? (
            <View>
              <SectionHeader
                title="Waiting on a confirm"
                caption="Each little confirms their own big, so there is nothing to do here but nudge"
              />
              <Card>
                <View style={{ gap: spacing.sm }}>
                  {pending.map((edge) => (
                    <View
                      key={edge.id}
                      style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}
                    >
                      <View
                        style={{
                          flex: 1,
                          flexDirection: "row",
                          alignItems: "center",
                          gap: spacing.xs,
                        }}
                      >
                        <AppText variant="body">{nameOf(edge.big_user_id)}</AppText>
                        <Feather name="arrow-right" size={13} color={palette.inkFaint} />
                        <AppText variant="body">{nameOf(edge.little_user_id)}</AppText>
                      </View>
                      <Chip label="Waiting to confirm" variant="warning" />
                    </View>
                  ))}
                </View>
              </Card>
            </View>
          ) : null}

          <Button label="Open the family tree" variant="ghost" onPress={() => router.push("/chapter/tree")} />
        </View>
      )}

      {chapterId !== null ? (
        <>
          <FamilySheet
            visible={familySheetOpen}
            onClose={() => setFamilySheetOpen(false)}
            chapterId={chapterId}
            existing={tree?.families ?? []}
            onSaved={() => void reload()}
          />
          <PairSheet
            visible={pairSheetOpen}
            onClose={() => setPairSheetOpen(false)}
            chapterId={chapterId}
            tree={tree}
            members={members}
            initialLittleId={pairLittleId}
            onSaved={onEdgeSaved}
          />
        </>
      ) : null}
    </Screen>
  );
}

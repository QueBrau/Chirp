/** Family tree: neural-network lineage graph (one big per little; pan / zoom / tap).
 *
 * c79 gives this screen its write path:
 * - A little with an unconfirmed big sees a confirm banner HERE, on the screen
 *   they already open — little-confirms-big is theirs alone (server-enforced).
 * - lineage_admin holders (e-board incl. historian) get pair/reassign/unpair on
 *   the canvas via NodeDetail's actions, all through the shared PairSheet. The
 *   list fallback (works with zero edges) lives on /chapter/historian.
 */

import { useRouter } from "expo-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, View } from "react-native";

import { listMembers, type MemberOut } from "@/api/chapters";
import {
  confirmEdge,
  deleteEdge,
  getLineage,
  type LineageEdgeOut,
  type LineageTreeOut,
} from "@/api/lineage";
import { useSession } from "@/auth";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { AppText, Button, EmptyState, Screen } from "@/components";
import { confirmAction, showApiError } from "@/lib/alert";
import { radii, spacing, useTheme } from "@/theme";
import { muli, useMuliFonts } from "@/tree/fonts";
import { unplacedCount } from "@/tree/layout";
import { NodeDetail } from "@/tree/NodeDetail";
import { PairSheet } from "@/tree/PairSheet";
import { TreeCanvas } from "@/tree/TreeCanvas";

const muliRegular = { fontFamily: muli.regular };
const muliMedium = { fontFamily: muli.medium };
const muliBold = { fontFamily: muli.bold };

export default function TreeScreen() {
  const palette = useTheme();
  const router = useRouter();
  const fontsLoaded = useMuliFonts();
  const { user } = useSession();
  const { sessionStatus, membership, chapterLoading, roleMeta } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;
  const [tree, setTree] = useState<LineageTreeOut | null>(null);
  // Tracked separately from `tree` so "still fetching" and "fetch settled,
  // no lineage" render as two distinct EmptyStates instead of collapsing.
  const [treeLoading, setTreeLoading] = useState(chapterId !== null);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  // c79 write path (lineage_admin only): roster for the PairSheet's pickers.
  const canEdit = roleMeta?.capabilities.includes("lineage_admin") ?? false;
  const [members, setMembers] = useState<MemberOut[]>([]);
  const [pairSheetOpen, setPairSheetOpen] = useState(false);
  const [pairLittleId, setPairLittleId] = useState<string | null>(null);
  const [pairBigId, setPairBigId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (chapterId === null) {
      setTree(null);
      setTreeLoading(false);
      return;
    }
    setTreeLoading(true);
    // Fail soft: previously this had no .catch(), so a real chapter id's
    // request rejecting (or the old mock id 422ing) left the screen stuck on
    // "Loading lineage..." forever with an unhandled rejection.
    try {
      setTree(await getLineage(chapterId));
    } catch {
      setTree(null);
    } finally {
      setTreeLoading(false);
    }
  }, [chapterId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!canEdit || chapterId === null) return;
    listMembers(chapterId)
      .then(setMembers)
      .catch(() => setMembers([]));
  }, [canEdit, chapterId]);

  /** The signed-in user's own unconfirmed edge — the confirm is theirs alone. */
  const myPendingEdge = useMemo(() => {
    if (!tree || user === null) return null;
    return (
      tree.edges.find(
        (edge) => edge.little_user_id === user.id && !edge.confirmed_by_little,
      ) ?? null
    );
  }, [tree, user]);

  const nameOf = useCallback(
    (userId: string): string =>
      tree?.nodes.find((node) => node.user_id === userId)?.display_name ?? "Unknown",
    [tree],
  );

  const confirmMyBig = async () => {
    if (chapterId === null || myPendingEdge === null) return;
    setConfirming(true);
    try {
      const updated = await confirmEdge(chapterId, myPendingEdge.id);
      setTree((current) =>
        current === null
          ? current
          : {
              ...current,
              edges: current.edges.map((edge) => (edge.id === updated.id ? updated : edge)),
            },
      );
    } catch (error) {
      showApiError(error, "Couldn't confirm");
    } finally {
      setConfirming(false);
    }
  };

  const openPair = (littleId: string | null, bigId: string | null) => {
    setPairLittleId(littleId);
    setPairBigId(bigId);
    setPairSheetOpen(true);
  };

  const confirmUnpair = (edge: LineageEdgeOut) => {
    if (chapterId === null) return;
    confirmAction({
      title: "Remove this pairing?",
      message:
        `${nameOf(edge.big_user_id)} stops being ${nameOf(edge.little_user_id)}'s big. ` +
        "The tree redraws without the branch; nothing else is deleted.",
      confirmLabel: "Remove",
      cancelLabel: "Keep it",
      destructive: true,
      onConfirm: () => {
        void (async () => {
          try {
            await deleteEdge(chapterId, edge.id);
            await load();
          } catch (error) {
            showApiError(error, "Couldn't remove it");
          }
        })();
      },
    });
  };

  if (!fontsLoaded) {
    return (
      <Screen scroll={false}>
        <EmptyState title="Loading lineage..." />
      </Screen>
    );
  }

  // Session-status gating (matches members.tsx): a real member's tree must
  // never flash "No lineage yet" while the session/chapter/tree are still
  // resolving.
  const loading = sessionStatus === "loading" || (membership !== null && chapterLoading) || treeLoading;

  const pending = tree?.edges.filter((e) => !e.confirmed_by_little).length ?? 0;
  const unplaced = tree ? unplacedCount(tree) : 0;
  // layoutLineageGraph only places nodes that appear in an edge, so with no
  // edges the canvas renders an empty box. Guarding on nodes.length alone let
  // that through: a chapter with members but no pairs yet got a blank card and
  // a bare "N unplaced" caption. That is not an edge case, it is the FIRST-RUN
  // state of every chapter — members exist before any lineage is recorded.
  const nothingToDraw = tree === null || tree.edges.length === 0;

  return (
    <Screen scroll>
      <View style={{ marginBottom: spacing.md, gap: spacing.xs }}>
        <AppText variant="display" style={muliBold}>
          Family Tree
        </AppText>
        <AppText variant="caption" tone="secondary" style={muliRegular}>
          One big per little · tap a node to focus
        </AppText>
      </View>

      {loading ? (
        <EmptyState title="Loading lineage..." />
      ) : nothingToDraw ? (
        <EmptyState
          title="No lineage yet"
          message={
            unplaced > 0
              ? `${unplaced} ${unplaced === 1 ? "member is" : "members are"} in the chapter, but no bigs and littles have been paired yet. Pairs show up here as soon as they are.`
              : "Bigs and littles will show up here once your chapter starts pairing them."
          }
          actionLabel={canEdit ? "Pair the first big and little" : undefined}
          onAction={canEdit ? () => openPair(null, null) : undefined}
        />
      ) : (
        <View style={{ gap: spacing.md }}>
          {myPendingEdge !== null ? (
            <View
              style={{
                gap: spacing.sm,
                padding: spacing.lg,
                borderRadius: radii.card,
                backgroundColor: palette.surface,
                borderWidth: 1,
                borderColor: palette.accent,
              }}
            >
              <AppText variant="micro" tone="accent" style={muliMedium}>
                YOUR BIG
              </AppText>
              <AppText variant="headline" style={muliBold}>
                Is {nameOf(myPendingEdge.big_user_id)} your big?
              </AppText>
              <AppText variant="caption" tone="secondary" style={muliRegular}>
                Only you can confirm this. Wrong person? Your e-board can reassign it.
              </AppText>
              <Button
                label={confirming ? "Confirming..." : "That's my big"}
                onPress={() => void confirmMyBig()}
                disabled={confirming}
              />
            </View>
          ) : null}

          <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
            {tree.families.map((family) => (
              <View
                key={family.id}
                style={{
                  flexDirection: "row",
                  alignItems: "center",
                  gap: spacing.xs,
                  paddingHorizontal: spacing.sm,
                  paddingVertical: spacing.xs,
                  borderRadius: radii.pill,
                  backgroundColor: palette.surface,
                  borderWidth: 1,
                  borderColor: palette.border,
                }}
              >
                <View
                  style={{
                    width: spacing.sm,
                    height: spacing.sm,
                    borderRadius: radii.pill,
                    backgroundColor: family.color,
                  }}
                />
                <AppText variant="caption" tone="secondary" style={muliMedium}>
                  {family.name}
                </AppText>
              </View>
            ))}
          </View>

          <TreeCanvas
            tree={tree}
            selectedUserId={selectedUserId}
            onSelectUser={setSelectedUserId}
          />

          {selectedUserId && chapterId !== null ? (
            <NodeDetail
              tree={tree}
              userId={selectedUserId}
              onClose={() => setSelectedUserId(null)}
              chapterId={chapterId}
              eboard={roleMeta?.eboard ?? []}
              canEdit={canEdit}
              onChangeBig={(littleId) => openPair(littleId, null)}
              onAddLittle={(bigId) => openPair(null, bigId)}
              onUnpair={confirmUnpair}
            />
          ) : canEdit && unplaced > 0 ? (
            // The old dead "N unplaced" caption, made actionable for the people
            // who can actually fix it — the list fallback owns the pairing UI.
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Open the historian tools"
              onPress={() => router.push("/chapter/historian")}
              style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
            >
              <AppText variant="caption" tone="accent" style={muliMedium}>
                {[
                  pending > 0 ? `${pending} unconfirmed` : null,
                  `${unplaced} unplaced, pair them`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </AppText>
            </Pressable>
          ) : (
            <AppText variant="caption" tone="tertiary" style={muliRegular}>
              {[
                pending > 0 ? `${pending} unconfirmed` : null,
                unplaced > 0 ? `${unplaced} unplaced` : null,
              ]
                .filter(Boolean)
                .join(" · ") || "All lineage edges confirmed"}
            </AppText>
          )}
        </View>
      )}

      {chapterId !== null && canEdit ? (
        <PairSheet
          visible={pairSheetOpen}
          onClose={() => setPairSheetOpen(false)}
          chapterId={chapterId}
          tree={tree}
          members={members}
          initialLittleId={pairLittleId}
          initialBigId={pairBigId}
          onSaved={() => {
            void load();
          }}
        />
      ) : null}
    </Screen>
  );
}

/** Family tree: neural-network lineage graph (one big per little; pan / zoom / tap). */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { getLineage, type LineageTreeOut } from "@/api/lineage";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { AppText, EmptyState, Screen } from "@/components";
import { radii, spacing, useTheme } from "@/theme";
import { muli, useMuliFonts } from "@/tree/fonts";
import { unplacedCount } from "@/tree/layout";
import { NodeDetail } from "@/tree/NodeDetail";
import { TreeCanvas } from "@/tree/TreeCanvas";

const muliRegular = { fontFamily: muli.regular };
const muliMedium = { fontFamily: muli.medium };
const muliBold = { fontFamily: muli.bold };

export default function TreeScreen() {
  const palette = useTheme();
  const fontsLoaded = useMuliFonts();
  const { sessionStatus, membership, chapterLoading } = useOwnChapter();
  const chapterId = membership?.chapter_id ?? null;
  const [tree, setTree] = useState<LineageTreeOut | null>(null);
  // Tracked separately from `tree` so "still fetching" and "fetch settled,
  // no lineage" render as two distinct EmptyStates instead of collapsing.
  const [treeLoading, setTreeLoading] = useState(chapterId !== null);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);

  useEffect(() => {
    if (chapterId === null) {
      setTree(null);
      setTreeLoading(false);
      return;
    }
    setTreeLoading(true);
    // Fail soft: previously this had no .catch(), so a real chapter id's
    // request rejecting (or the old mock id 422ing) left the screen stuck on
    // "Loading lineage..." forever with an unhandled rejection.
    getLineage(chapterId)
      .then(setTree)
      .catch(() => setTree(null))
      .finally(() => setTreeLoading(false));
  }, [chapterId]);

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
        />
      ) : (
        <View style={{ gap: spacing.md }}>
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

          {selectedUserId ? (
            <NodeDetail
              tree={tree}
              userId={selectedUserId}
              onClose={() => setSelectedUserId(null)}
            />
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
    </Screen>
  );
}

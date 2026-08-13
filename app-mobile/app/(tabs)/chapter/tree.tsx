/** Family tree: neural-network lineage graph (one big per little; pan / zoom / tap). */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { getLineage, type LineageTreeOut } from "@/api/lineage";
import { AppText, EmptyState, Screen } from "@/components";
import { MOCK_CURRENT_MEMBERSHIP } from "@/mocks/data";
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
  const [tree, setTree] = useState<LineageTreeOut | null>(null);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);

  useEffect(() => {
    void getLineage(MOCK_CURRENT_MEMBERSHIP.chapter_id).then(setTree);
  }, []);

  if (!fontsLoaded) {
    return (
      <Screen scroll={false}>
        <EmptyState title="Loading lineage..." />
      </Screen>
    );
  }

  const pending = tree?.edges.filter((e) => !e.confirmed_by_little).length ?? 0;
  const unplaced = tree ? unplacedCount(tree) : 0;

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

      {tree === null ? (
        <EmptyState title="Loading lineage..." />
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

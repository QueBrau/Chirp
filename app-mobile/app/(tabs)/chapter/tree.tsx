/** Family tree PLACEHOLDER: families + edges as text. Interactive Skia canvas is milestone 6. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import { getLineage, type LineageTreeOut } from "@/api/lineage";
import { AppText, Badge, Card, EmptyState, Screen } from "@/components";
import { MOCK_CURRENT_MEMBERSHIP } from "@/mocks/data";
import { radii, spacing } from "@/theme";

export default function TreeScreen() {
  const [tree, setTree] = useState<LineageTreeOut | null>(null);

  useEffect(() => {
    void getLineage(MOCK_CURRENT_MEMBERSHIP.chapter_id).then(setTree);
  }, []);

  const nodeName = (userId: string): string => {
    const node = tree?.nodes.find((n) => n.user_id === userId);
    if (!node) return "Unknown";
    return node.is_ghost ? `${node.display_name} (ghost)` : node.display_name;
  };

  return (
    <Screen
      title="Family Tree"
      subtitle="Text placeholder — the interactive Skia tree ships in milestone 6"
    >
      {tree === null ? (
        <EmptyState title="Loading lineage..." />
      ) : (
        <View style={{ gap: spacing.md }}>
          {tree.families.map((family) => {
            const familyEdges = tree.edges.filter((edge) => edge.family_id === family.id);
            return (
              <Card key={family.id}>
                <View style={{ gap: spacing.sm }}>
                  <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
                    {/* Family color comes from API data (families.color), not the style system. */}
                    <View
                      style={{
                        width: spacing.md,
                        height: spacing.md,
                        borderRadius: radii.pill,
                        backgroundColor: family.color,
                      }}
                    />
                    <AppText variant="title">{family.name}</AppText>
                  </View>
                  {familyEdges.map((edge) => (
                    <View
                      key={edge.id}
                      style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}
                    >
                      <AppText style={{ flexShrink: 1 }}>
                        {nodeName(edge.big_user_id)} → {nodeName(edge.little_user_id)}
                      </AppText>
                      {!edge.confirmed_by_little ? (
                        <Badge label="Unconfirmed" tone="danger" />
                      ) : null}
                    </View>
                  ))}
                  <AppText variant="caption" tone="tertiary">
                    {familyEdges.length} {familyEdges.length === 1 ? "edge" : "edges"}
                  </AppText>
                </View>
              </Card>
            );
          })}

          <Card>
            <View style={{ gap: spacing.sm }}>
              <AppText variant="title">Unplaced members</AppText>
              {tree.nodes
                .filter((node) => node.family_id === null)
                .map((node) => (
                  <AppText key={node.user_id} tone="secondary">
                    {node.display_name}
                    {node.pledge_class ? ` — ${node.pledge_class}` : ""}
                  </AppText>
                ))}
            </View>
          </Card>

          {/* TODO(milestone-6): react-native-skia + d3-hierarchy canvas with pan/pinch/tap. */}
        </View>
      )}
    </Screen>
  );
}

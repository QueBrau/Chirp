/**
 * Family tree PLACEHOLDER (DESIGN §7): family Chips in family colors, indented
 * big-to-little rows built from the edge list. Interactive Skia canvas ships
 * in milestone 6 — this screen stays a styled text placeholder until then.
 */

import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { View } from "react-native";

import { getLineage, type LineageEdgeOut, type LineageTreeOut } from "@/api/lineage";
import { AppText, Card, Chip, EmptyState, GradientAvatar, ListRow, Screen, SectionHeader } from "@/components";
import { MOCK_CURRENT_MEMBERSHIP, mockUserById } from "@/mocks/data";
import { radii, spacing } from "@/theme";

/** Indent step per generation (big → little). */
const INDENT_STEP = spacing.xl;

/** Family name pill tinted with the family's own color (API data, not the style system). */
function FamilyChip({ name, color }: { name: string; color: string }) {
  return (
    <View
      style={{
        alignSelf: "flex-start",
        borderRadius: radii.pill,
        paddingHorizontal: spacing.md,
        paddingVertical: spacing.xs,
        backgroundColor: `${color}1F`,
      }}
    >
      <AppText variant="micro" style={{ color }}>
        {name}
      </AppText>
    </View>
  );
}

/** One node in the indented big→little chain; recurses into its own littles. */
function LineageRow({
  userId,
  depth,
  pending,
  familyColor,
  childrenByBig,
  nodeName,
}: {
  userId: string;
  depth: number;
  pending: boolean;
  familyColor: string;
  childrenByBig: Map<string, LineageEdgeOut[]>;
  nodeName: (userId: string) => string;
}): ReactNode {
  const childEdges = childrenByBig.get(userId) ?? [];

  return (
    <View key={userId} style={{ gap: spacing.sm }}>
      <View
        style={{
          flexDirection: "row",
          alignItems: "center",
          gap: spacing.sm,
          paddingLeft: depth * INDENT_STEP,
        }}
      >
        {depth > 0 ? (
          <View
            style={{
              width: spacing.sm,
              height: spacing.sm,
              borderRadius: radii.pill,
              backgroundColor: `${familyColor}55`,
            }}
          />
        ) : null}
        <AppText variant={depth === 0 ? "headline" : "body"} tone={depth === 0 ? "primary" : "secondary"}>
          {nodeName(userId)}
        </AppText>
        {pending ? <Chip label="Unconfirmed" variant="warning" /> : null}
      </View>
      {childEdges.map((edge) => (
        <LineageRow
          key={edge.id}
          userId={edge.little_user_id}
          depth={depth + 1}
          pending={!edge.confirmed_by_little}
          familyColor={familyColor}
          childrenByBig={childrenByBig}
          nodeName={nodeName}
        />
      ))}
    </View>
  );
}

/** Roots of a family's chain: bigs who are never someone else's little. */
function findRoots(edges: LineageEdgeOut[]): string[] {
  const littles = new Set(edges.map((edge) => edge.little_user_id));
  const bigs = new Set(edges.map((edge) => edge.big_user_id));
  return [...bigs].filter((id) => !littles.has(id));
}

function groupByBig(edges: LineageEdgeOut[]): Map<string, LineageEdgeOut[]> {
  const map = new Map<string, LineageEdgeOut[]>();
  for (const edge of edges) {
    const list = map.get(edge.big_user_id) ?? [];
    list.push(edge);
    map.set(edge.big_user_id, list);
  }
  return map;
}

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

  const unplaced = tree?.nodes.filter((node) => node.family_id === null) ?? [];

  return (
    <Screen title="Family Tree" subtitle="Bigs, littles, and lineage">
      {tree === null ? (
        <EmptyState title="Loading lineage..." />
      ) : (
        <View style={{ gap: spacing.xl }}>
          {tree.families.map((family) => {
            const familyEdges = tree.edges.filter((edge) => edge.family_id === family.id);
            const childrenByBig = groupByBig(familyEdges);
            const roots = findRoots(familyEdges);

            return (
              <Card key={family.id}>
                <View style={{ gap: spacing.md }}>
                  <View
                    style={{
                      flexDirection: "row",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: spacing.sm,
                    }}
                  >
                    <FamilyChip name={family.name} color={family.color} />
                    <AppText variant="caption" tone="tertiary">
                      {familyEdges.length} {familyEdges.length === 1 ? "edge" : "edges"}
                    </AppText>
                  </View>
                  <View style={{ gap: spacing.md }}>
                    {roots.map((rootId) => (
                      <LineageRow
                        key={rootId}
                        userId={rootId}
                        depth={0}
                        pending={false}
                        familyColor={family.color}
                        childrenByBig={childrenByBig}
                        nodeName={nodeName}
                      />
                    ))}
                  </View>
                </View>
              </Card>
            );
          })}

          {unplaced.length > 0 ? (
            <View>
              <SectionHeader
                title="Unplaced"
                caption="Not yet assigned to a family"
              />
              <Card>
                {unplaced.map((node, index) => (
                  <ListRow
                    key={node.user_id}
                    title={node.display_name}
                    subtitle={node.pledge_class ?? undefined}
                    left={
                      <GradientAvatar
                        name={node.display_name}
                        size={40}
                        photoUrl={mockUserById(node.user_id)?.avatar_url}
                      />
                    }
                    divider={index < unplaced.length - 1}
                  />
                ))}
              </Card>
            </View>
          ) : null}

          <View style={{ alignItems: "center", paddingTop: spacing.sm, paddingBottom: spacing.lg }}>
            <AppText variant="caption" tone="tertiary" style={{ textAlign: "center" }}>
              This is a text placeholder — the interactive pan/pinch/tap tree canvas ships in
              milestone 6.
            </AppText>
          </View>
        </View>
      )}
    </Screen>
  );
}

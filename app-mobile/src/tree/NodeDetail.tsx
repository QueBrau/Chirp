/** Selected-node inspector under the graph: identity, family, big/littles. */

import { Pressable, View } from "react-native";

import type { LineageTreeOut } from "@/api/lineage";
import { AppText, Avatar, Badge, Card } from "@/components";
import { radii, spacing, useTheme } from "@/theme";

import { muli } from "./fonts";
import { edgesForUser } from "./layout";

export interface NodeDetailProps {
  tree: LineageTreeOut;
  userId: string;
  onClose: () => void;
}

const muliRegular = { fontFamily: muli.regular };
const muliMedium = { fontFamily: muli.medium };
const muliSemibold = { fontFamily: muli.semibold };

export function NodeDetail({ tree, userId, onClose }: NodeDetailProps) {
  const palette = useTheme();
  const node = tree.nodes.find((n) => n.user_id === userId);
  if (!node) return null;

  const family = tree.families.find((f) => f.id === node.family_id) ?? null;
  const edges = edgesForUser(tree, userId);
  const bigs = edges
    .filter((e) => e.little_user_id === userId)
    .map((e) => ({
      edge: e,
      person: tree.nodes.find((n) => n.user_id === e.big_user_id),
    }));
  const littles = edges
    .filter((e) => e.big_user_id === userId)
    .map((e) => ({
      edge: e,
      person: tree.nodes.find((n) => n.user_id === e.little_user_id),
    }));

  return (
    <Card>
      <View style={{ gap: spacing.md }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.md }}>
          <Avatar name={node.display_name} ghost={node.is_ghost} size={44} />
          <View style={{ flex: 1, gap: 2 }}>
            <AppText variant="title" style={muliSemibold}>
              {node.display_name}
            </AppText>
            <AppText variant="caption" tone="secondary" style={muliRegular}>
              {[node.pledge_class, family?.name, node.is_ghost ? "Ghost" : null]
                .filter(Boolean)
                .join(" · ")}
            </AppText>
          </View>
          <Pressable onPress={onClose} hitSlop={12}>
            <AppText tone="tertiary" variant="caption" style={muliMedium}>
              Close
            </AppText>
          </Pressable>
        </View>

        {family ? (
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
            <View
              style={{
                width: spacing.md,
                height: spacing.md,
                borderRadius: radii.pill,
                backgroundColor: family.color,
              }}
            />
            <AppText variant="caption" tone="secondary" style={muliRegular}>
              {family.name}
            </AppText>
            {node.is_ghost ? <Badge label="Historical" tone="neutral" /> : null}
          </View>
        ) : (
          <AppText variant="caption" tone="tertiary" style={muliRegular}>
            Not placed in a family yet
          </AppText>
        )}

        <View style={{ gap: spacing.sm }}>
          <AppText variant="caption" tone="tertiary" style={muliMedium}>
            BIG (one per member)
          </AppText>
          {bigs.length === 0 ? (
            <AppText tone="secondary" style={muliRegular}>
              No big on record
            </AppText>
          ) : (
            bigs.slice(0, 1).map(({ edge, person }) => (
              <View
                key={edge.id}
                style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}
              >
                <AppText style={[{ flex: 1 }, muliRegular]}>
                  {person?.display_name ?? "Unknown"}
                </AppText>
                {!edge.confirmed_by_little ? <Badge label="Unconfirmed" tone="danger" /> : null}
              </View>
            ))
          )}
        </View>

        <View style={{ gap: spacing.sm }}>
          <AppText variant="caption" tone="tertiary" style={muliMedium}>
            LITTLES
          </AppText>
          {littles.length === 0 ? (
            <AppText tone="secondary" style={muliRegular}>
              No littles yet
            </AppText>
          ) : (
            littles.map(({ edge, person }) => (
              <View
                key={edge.id}
                style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}
              >
                <AppText style={[{ flex: 1 }, muliRegular]}>
                  {person?.display_name ?? "Unknown"}
                </AppText>
                {!edge.confirmed_by_little ? <Badge label="Pending" tone="danger" /> : null}
              </View>
            ))
          )}
        </View>

        <AppText
          variant="caption"
          tone="tertiary"
          style={[muliRegular, { color: palette.textTertiary }]}
        >
          Drag to pan · pinch or scroll to zoom · tap a node to focus its lineage
        </AppText>
      </View>
    </Card>
  );
}

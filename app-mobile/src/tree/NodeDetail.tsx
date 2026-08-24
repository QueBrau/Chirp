/** Selected-node inspector under the graph: identity, family, big/littles —
 * and, for lineage_admin holders (c79), the canvas-side write actions: set or
 * change this person's big, add a little under them, unpair a wrong edge. */

import { Pressable, View } from "react-native";

import type { LineageEdgeOut, LineageTreeOut } from "@/api/lineage";
import { AppText, Avatar, Badge, Card, Chip } from "@/components";
import { radii, spacing, useTheme } from "@/theme";

import { muli } from "./fonts";
import { edgesForUser } from "./layout";

export interface NodeDetailProps {
  tree: LineageTreeOut;
  userId: string;
  onClose: () => void;
  /** lineage_admin only — absent for everyone else, and the actions with it. */
  canEdit?: boolean;
  /** Open the PairSheet with this person as the little (set or change their big). */
  onChangeBig?: (littleUserId: string) => void;
  /** Open the PairSheet with this person as the big (add a little under them). */
  onAddLittle?: (bigUserId: string) => void;
  /** Pure unpair (DELETE edge) — the caller owns the confirm dialog. */
  onUnpair?: (edge: LineageEdgeOut) => void;
}

const muliRegular = { fontFamily: muli.regular };
const muliMedium = { fontFamily: muli.medium };
const muliSemibold = { fontFamily: muli.semibold };

export function NodeDetail({
  tree,
  userId,
  onClose,
  canEdit = false,
  onChangeBig,
  onAddLittle,
  onUnpair,
}: NodeDetailProps) {
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
            <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
              <AppText tone="secondary" style={[{ flex: 1 }, muliRegular]}>
                No big on record
              </AppText>
              {canEdit && onChangeBig && !node.is_ghost ? (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={`Set ${node.display_name}'s big`}
                  onPress={() => onChangeBig(userId)}
                  style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
                >
                  <Chip label="Set big" variant="accent" />
                </Pressable>
              ) : null}
            </View>
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
                {canEdit && onChangeBig ? (
                  <Pressable
                    accessibilityRole="button"
                    accessibilityLabel={`Change ${node.display_name}'s big`}
                    onPress={() => onChangeBig(userId)}
                    style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
                  >
                    <Chip label="Change" variant="neutral" />
                  </Pressable>
                ) : null}
                {canEdit && onUnpair ? (
                  <Pressable
                    accessibilityRole="button"
                    accessibilityLabel={`Remove ${node.display_name}'s big`}
                    onPress={() => onUnpair(edge)}
                    style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
                  >
                    <Chip label="Remove" variant="danger" />
                  </Pressable>
                ) : null}
              </View>
            ))
          )}
        </View>

        <View style={{ gap: spacing.sm }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
            <AppText variant="caption" tone="tertiary" style={[{ flex: 1 }, muliMedium]}>
              LITTLES
            </AppText>
            {canEdit && onAddLittle ? (
              <Pressable
                accessibilityRole="button"
                accessibilityLabel={`Add a little under ${node.display_name}`}
                onPress={() => onAddLittle(userId)}
                style={({ pressed }) => ({ opacity: pressed ? 0.7 : 1 })}
              >
                <Chip label="Add little" variant="accent" />
              </Pressable>
            ) : null}
          </View>
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

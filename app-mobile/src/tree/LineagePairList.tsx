/** Accessible lineage list: the pairs TreeCanvas draws, as real text (c334).
 *
 * The canvas is an SVG surface whose nodes carry onPress handlers no screen
 * reader can reach, so tree.tsx hides that subtree from assistive tech and
 * points it here instead. That makes this list the ONLY way a VoiceOver user
 * selects a member, so every row is a real button wired to the same
 * onSelectUser the canvas nodes call. Hiding the drawing without replacing the
 * interaction would have cost those users the pairing flows outright.
 *
 * Drawing decorative, data in real text: the BalanceTrend/CategoryDonut
 * precedent (DESIGN.md §11). This reads tree.edges and tree.nodes directly, so
 * there is no second fetch and no second shape, and the list and the drawing
 * cannot disagree about who is whose big.
 */

import { useMemo } from "react";
import { Pressable, View } from "react-native";

import { AppText } from "@/components/AppText";
import { Avatar } from "@/components/Avatar";
import { Chip } from "@/components/Chip";
import { SectionHeader } from "@/components/SectionHeader";
import { radii, spacing, useTheme } from "@/theme";
import type { LineageEdgeOut, LineageNodeOut, LineageTreeOut } from "@/api/lineage";

import { muli } from "./fonts";

const muliRegular = { fontFamily: muli.regular };
const muliMedium = { fontFamily: muli.medium };
const muliBold = { fontFamily: muli.bold };

/** Edges of one family, or the trailing unfiled group when familyId is null. */
interface PairGroup {
  familyId: string | null;
  /** Header text. */
  name: string;
  /** What a screen reader says, which is not always the header plus a word:
   *  "Other family" + " family" read as "Other family family". */
  spokenName: string;
  color: string | null;
  edges: LineageEdgeOut[];
}

export interface LineagePairListProps {
  tree: LineageTreeOut;
  selectedUserId: string | null;
  onSelectUser: (userId: string | null) => void;
}

export function LineagePairList({ tree, selectedUserId, onSelectUser }: LineagePairListProps) {
  const palette = useTheme();

  const nodeById = useMemo(() => {
    const map = new Map<string, LineageNodeOut>();
    for (const node of tree.nodes) map.set(node.user_id, node);
    return map;
  }, [tree.nodes]);

  const groups = useMemo<PairGroup[]>(() => {
    const byFamily = new Map<string | null, LineageEdgeOut[]>();
    for (const edge of tree.edges) {
      const key = edge.family_id;
      const bucket = byFamily.get(key);
      if (bucket) bucket.push(edge);
      else byFamily.set(key, [edge]);
    }

    const nameOf = (id: string): string => nodeById.get(id)?.display_name ?? "Unknown member";
    // Alphabetical by big, then little: a screen reader user walks this list
    // linearly, so a stable order matters more here than on the canvas.
    const sortEdges = (edges: LineageEdgeOut[]): LineageEdgeOut[] =>
      [...edges].sort(
        (a, b) =>
          nameOf(a.big_user_id).localeCompare(nameOf(b.big_user_id)) ||
          nameOf(a.little_user_id).localeCompare(nameOf(b.little_user_id)),
      );

    const out: PairGroup[] = [];
    for (const family of tree.families) {
      const edges = byFamily.get(family.id);
      if (edges && edges.length > 0) {
        out.push({
          familyId: family.id,
          name: family.name,
          spokenName: `${family.name} family`,
          color: family.color,
          edges: sortEdges(edges),
        });
      }
    }
    // Families the tree does not know about still have to render. Dropping them
    // would make the list quietly shorter than the canvas, which is the exact
    // disagreement this component exists to prevent.
    for (const [familyId, edges] of byFamily) {
      if (familyId === null) continue;
      if (tree.families.some((f) => f.id === familyId)) continue;
      out.push({
        familyId,
        name: "Other family",
        spokenName: "Another family",
        color: null,
        edges: sortEdges(edges),
      });
    }
    const unfiled = byFamily.get(null);
    if (unfiled && unfiled.length > 0) {
      out.push({
        familyId: null,
        name: "No family yet",
        spokenName: "No family yet",
        color: null,
        edges: sortEdges(unfiled),
      });
    }
    return out;
  }, [tree.edges, tree.families, nodeById]);

  return (
    <View style={{ gap: spacing.md }} accessibilityLabel="Bigs and littles">
      <SectionHeader
        title="Every pair"
        caption={`${tree.edges.length} ${tree.edges.length === 1 ? "pair" : "pairs"}`}
      />

      {groups.map((group) => (
        <View key={group.familyId ?? "__unfiled"} style={{ gap: spacing.xs }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs }}>
            {group.color !== null ? (
              <View
                style={{
                  width: spacing.sm,
                  height: spacing.sm,
                  borderRadius: radii.pill,
                  backgroundColor: group.color,
                }}
              />
            ) : null}
            <AppText variant="micro" tone="tertiary" style={muliMedium}>
              {group.name.toUpperCase()}
            </AppText>
          </View>

          {group.edges.map((edge) => {
            const big = nodeById.get(edge.big_user_id);
            const little = nodeById.get(edge.little_user_id);
            const bigName = big?.display_name ?? "Unknown member";
            const littleName = little?.display_name ?? "Unknown member";
            // Two different ideas, deliberately not one. `involves` is visual
            // affinity: focusing a member lights up every pair they appear in,
            // the way the canvas dims everything else. `isSelected` is the
            // a11y state, and it tracks THIS row's press target only. Reusing
            // `involves` for accessibilityState announced a row as selected
            // that the user had never pressed.
            const involves =
              selectedUserId === edge.little_user_id || selectedUserId === edge.big_user_id;
            const isSelected = selectedUserId === edge.little_user_id;

            const spoken = [
              group.spokenName,
              edge.pledge_class,
              edge.confirmed_by_little ? null : "Not confirmed yet",
            ].filter(Boolean) as string[];

            return (
              <Pressable
                key={edge.id}
                accessibilityRole="button"
                accessibilityState={{ selected: isSelected }}
                accessibilityLabel={`${bigName} is ${littleName}'s big. ${spoken.join(". ")}`}
                accessibilityHint={`Shows ${littleName}'s lineage details`}
                onPress={() => onSelectUser(isSelected ? null : edge.little_user_id)}
                style={({ pressed }) => ({
                  flexDirection: "row",
                  alignItems: "center",
                  gap: spacing.sm,
                  // 44pt is the HIG floor; verify-a11y-touch only scans three
                  // named component files, so this row is asserted in
                  // verify-lineage-a11y.mjs instead of being assumed covered.
                  minHeight: 48,
                  padding: spacing.sm,
                  paddingRight: spacing.md,
                  borderRadius: radii.card,
                  backgroundColor: involves ? palette.accentSoft : palette.surface,
                  borderWidth: 1,
                  borderColor: involves ? palette.accent : palette.border,
                  opacity: pressed ? 0.7 : 1,
                })}
              >
                <View
                  style={{
                    alignSelf: "stretch",
                    width: 3,
                    borderRadius: radii.pill,
                    backgroundColor: group.color ?? palette.border,
                  }}
                />

                <Avatar name={bigName} size={32} ghost={big?.is_ghost ?? false} />

                <View style={{ flex: 1, gap: 2 }}>
                  <AppText variant="body" numberOfLines={1} style={muliBold}>
                    {bigName}
                  </AppText>
                  <AppText variant="caption" numberOfLines={1} style={muliRegular}>
                    <AppText variant="caption" tone="tertiary" style={muliRegular}>
                      {"big to "}
                    </AppText>
                    {littleName}
                  </AppText>
                  {edge.pledge_class ? (
                    <AppText variant="micro" tone="tertiary" numberOfLines={1} style={muliMedium}>
                      {edge.pledge_class.toUpperCase()}
                    </AppText>
                  ) : null}
                </View>

                {edge.confirmed_by_little ? null : <Chip label="PENDING" variant="warning" />}
              </Pressable>
            );
          })}
        </View>
      ))}
    </View>
  );
}

/** Selected-node inspector under the graph: identity, family, big/littles —
 * and, for lineage_admin holders (c79), the canvas-side write actions: set or
 * change this person's big, add a little under them, unpair a wrong edge. */

import { useEffect, useState } from "react";
import { Pressable, View } from "react-native";

import { getRoleTerms, type RoleName, type RoleTerm } from "@/api/chapters";
import { ApiError } from "@/api/client";
import type { LineageEdgeOut, LineageTreeOut } from "@/api/lineage";
import { chipVariant, currentTerm, roleLabel, termDateLabel } from "@/lib/roleTerms";
import { AppText, Avatar, Badge, Card, Chip } from "@/components";
import { radii, spacing } from "@/theme";

import { muli } from "./fonts";
import { edgesForUser } from "./layout";

export interface NodeDetailProps {
  tree: LineageTreeOut;
  userId: string;
  onClose: () => void;
  /** For the compact role/term line (board card c181) — one fetch, for the
   * single selected node only (never per-row: the tree selects one node at a
   * time, so this is inherently the "fetch on tap" shape the directory's N+1
   * guard asks for). */
  chapterId: string;
  eboard: RoleName[];
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
  chapterId,
  eboard,
  canEdit = false,
  onChangeBig,
  onAddLittle,
  onUnpair,
}: NodeDetailProps) {
  const node = tree.nodes.find((n) => n.user_id === userId);

  // Current role + its honesty-gated date, fetched once per selection — not
  // once per row, since only one node is ever selected at a time. Ghosts have
  // no Membership row (lineage_service's "allowed ghost" concept), so
  // GET .../role-terms would 404 for them; skip the fetch and the line both.
  const [terms, setTerms] = useState<RoleTerm[] | null>(null);
  /** The role-terms fetch failed. Distinct from "this person holds no role" (c330). */
  const [termsFailed, setTermsFailed] = useState(false);
  useEffect(() => {
    setTerms(null);
    setTermsFailed(false);
    if (!node || node.is_ghost) return;
    getRoleTerms(chapterId, userId)
      .then(setTerms)
      // c330: this was `.catch(() => setTerms([]))`, and [] renders "No current role on
      // record" — a statement about the named person whose node you just tapped, made
      // out of a dropped request. Identical to c316's "may have left the chapter" on
      // member/[id].tsx, on the same endpoint, one screen over; c316 fixed the profile
      // and this tree sheet kept the bug.
      //
      // 404 is the one failure that IS the answer: list_role_terms raises
      // membership_not_found when the target has no membership row for this chapter
      // (backend/app/routers/chapters.py:629), so "no role" is then true. Everything
      // else is our failure to answer and must not be spoken as a fact about them.
      .catch((error: unknown) => {
        setTerms([]);
        setTermsFailed(!(error instanceof ApiError && error.status === 404));
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chapterId, userId, node?.is_ghost]);

  if (!node) return null;
  const current = terms ? currentTerm(terms) : null;
  const currentDateLabel = current ? termDateLabel(current) : null;

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

        {/* Compact current-role line (board card c181) — reuses c180's exact
         * date-honesty rule (@/lib/roleTerms) so a seeded/backfilled term
         * never shows an invented start date here either. Ghosts have no
         * Membership row, so they get no role line at all rather than a
         * doomed fetch. */}
        {!node.is_ghost ? (
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
            {current ? (
              <Chip label={roleLabel(current.role)} variant={chipVariant(current.role, eboard)} />
            ) : terms === null ? (
              <AppText variant="caption" tone="tertiary" style={muliRegular}>
                Loading role…
              </AppText>
            ) : termsFailed ? (
              // Ahead of the "no role" line, because `terms` is [] in both states and
              // whichever branch comes first wins — the ordering IS the fix (c299/c316).
              <AppText variant="caption" tone="tertiary" style={muliRegular}>
                Couldn't load role
              </AppText>
            ) : (
              <AppText variant="caption" tone="tertiary" style={muliRegular}>
                No current role on record
              </AppText>
            )}
            {currentDateLabel ? (
              <AppText variant="caption" tone="secondary" style={muliRegular}>
                {currentDateLabel}
              </AppText>
            ) : null}
          </View>
        ) : null}

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
                {!edge.confirmed_by_little ? <Badge label="Unconfirmed" tone="warning" /> : null}
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
                {!edge.confirmed_by_little ? <Badge label="Pending" tone="warning" /> : null}
              </View>
            ))
          )}
        </View>

        <AppText variant="caption" tone="tertiary" style={muliRegular}>
          Drag to pan · pinch or scroll to zoom · tap a node to focus its lineage
        </AppText>
      </View>
    </Card>
  );
}

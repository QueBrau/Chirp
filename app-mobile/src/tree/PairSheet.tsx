/**
 * PairSheet (c79): the ONE write surface for big/little edges — used from the
 * tree canvas (NodeDetail actions) and the historian screen's list fallback.
 *
 * Reassignment is a STATE here, not a toast: when the chosen little already has
 * a big (known from the tree, or discovered via the server's 409), the sheet
 * shows who the current big is and what saving will do, and the save switches
 * to the atomic replace call (createEdge with replace_existing) — never a
 * client-side delete-then-recreate, which could strand the little big-less on a
 * partial failure. Fixing a wrong big IS the common case per the card.
 */

import { Feather } from "@expo/vector-icons";
import { useEffect, useMemo, useState } from "react";
import { Modal, Pressable, ScrollView, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import type { MemberOut } from "@/api/chapters";
import { ApiError } from "@/api/client";
import { createEdge, type LineageEdgeOut, type LineageTreeOut } from "@/api/lineage";
import { AppText, Button, Chip, GradientAvatar } from "@/components";
import { apiErrorMessage } from "@/lib/alert";
import { light, radii, spacing, typography, useTheme, withAlpha } from "@/theme";

export interface PairSheetProps {
  visible: boolean;
  onClose: () => void;
  chapterId: string;
  /** Current tree — the source for existing-big detection, families, and ghosts. */
  tree: LineageTreeOut | null;
  /** Chapter roster — the only name source for members not yet on the tree. */
  members: MemberOut[];
  /** Prefill: pairing FOR this little (tree flow: "change big"). */
  initialLittleId?: string | null;
  /** Prefill: pairing FROM this big (tree flow: "add a little"). */
  initialBigId?: string | null;
  onSaved: (edge: LineageEdgeOut) => void;
}

interface Candidate {
  id: string;
  name: string;
  avatarUrl: string | null;
  isGhost: boolean;
}

type PickerSlot = "little" | "big";

/** Roster members (active only) plus ghost nodes already on the tree — a ghost
 * can be a big (historical lineage) even though they can never confirm. */
function buildCandidates(members: MemberOut[], tree: LineageTreeOut | null): Candidate[] {
  const fromRoster: Candidate[] = members
    .filter((member) => member.status === "active")
    .map((member) => ({
      id: member.user_id,
      name: member.display_name,
      avatarUrl: member.avatar_url,
      isGhost: false,
    }));
  const seen = new Set(fromRoster.map((candidate) => candidate.id));
  const ghosts: Candidate[] = (tree?.nodes ?? [])
    .filter((node) => node.is_ghost && !seen.has(node.user_id))
    .map((node) => ({
      id: node.user_id,
      name: node.display_name,
      avatarUrl: node.avatar_url,
      isGhost: true,
    }));
  return [...fromRoster, ...ghosts].sort((a, b) => a.name.localeCompare(b.name));
}

function SlotField({
  label,
  candidate,
  open,
  onPress,
}: {
  label: string;
  candidate: Candidate | null;
  open: boolean;
  onPress: () => void;
}) {
  const palette = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ expanded: open }}
      accessibilityLabel={`Choose ${label.toLowerCase()}`}
      onPress={onPress}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: spacing.md,
        padding: spacing.md,
        borderRadius: radii.input,
        backgroundColor: palette.surfaceAlt,
        borderWidth: 1,
        borderColor: open ? palette.accent : "transparent",
        opacity: pressed ? 0.85 : 1,
      })}
    >
      <View style={{ flex: 1, gap: 2 }}>
        <AppText variant="micro" tone="secondary">
          {label}
        </AppText>
        {candidate ? (
          <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.sm }}>
            <GradientAvatar name={candidate.name} size={24} photoUrl={candidate.avatarUrl ?? undefined} />
            <AppText variant="bodyBold">{candidate.name}</AppText>
            {candidate.isGhost ? <Chip label="Historical" variant="neutral" /> : null}
          </View>
        ) : (
          <AppText variant="body" tone="tertiary">
            Choose...
          </AppText>
        )}
      </View>
      <Feather name={open ? "chevron-up" : "chevron-down"} size={18} color={palette.inkFaint} />
    </Pressable>
  );
}

export function PairSheet({
  visible,
  onClose,
  chapterId,
  tree,
  members,
  initialLittleId = null,
  initialBigId = null,
  onSaved,
}: PairSheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();

  const [littleId, setLittleId] = useState<string | null>(initialLittleId);
  const [bigId, setBigId] = useState<string | null>(initialBigId);
  const [familyId, setFamilyId] = useState<string | null>(null);
  const [openSlot, setOpenSlot] = useState<PickerSlot | null>(null);
  const [filter, setFilter] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set when the server 409s a plain create — the tree we rendered from was
  // stale, so the conflict panel shows even though local data sees no big.
  const [conflictFromServer, setConflictFromServer] = useState(false);

  // Re-seed when the sheet opens for a different node (tap big A, close, tap big B).
  useEffect(() => {
    if (visible) {
      setLittleId(initialLittleId);
      setBigId(initialBigId);
      setFamilyId(null);
      setOpenSlot(null);
      setFilter("");
      setError(null);
      setConflictFromServer(false);
    }
  }, [visible, initialLittleId, initialBigId]);

  const candidates = useMemo(() => buildCandidates(members, tree), [members, tree]);
  const byId = useMemo(() => new Map(candidates.map((c) => [c.id, c])), [candidates]);

  const little = littleId ? (byId.get(littleId) ?? null) : null;
  const big = bigId ? (byId.get(bigId) ?? null) : null;

  /** The little's existing edge, if any — this is what makes the save a reassignment. */
  const existingEdge = useMemo(() => {
    if (!littleId || !tree) return null;
    return tree.edges.find((edge) => edge.little_user_id === littleId) ?? null;
  }, [littleId, tree]);

  const currentBigName = existingEdge
    ? (byId.get(existingEdge.big_user_id)?.name ??
      tree?.nodes.find((n) => n.user_id === existingEdge.big_user_id)?.display_name ??
      "someone")
    : null;

  const isReassign = conflictFromServer || (existingEdge !== null && existingEdge.big_user_id !== bigId);
  const alreadyPaired = existingEdge !== null && existingEdge.big_user_id === bigId;

  // Default the family to the big's own — that is what the tree would propagate
  // visually anyway, so the preselect matches what the canvas will draw.
  useEffect(() => {
    // Only fills an untouched field: familyId in the deps re-runs the effect
    // when the user picks one, and the guard makes that a no-op.
    if (!bigId || !tree || familyId !== null) return;
    const bigNode = tree.nodes.find((node) => node.user_id === bigId);
    if (bigNode?.family_id) setFamilyId(bigNode.family_id);
  }, [bigId, tree, familyId]);

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const excluded = openSlot === "little" ? bigId : littleId;
    return candidates.filter(
      (candidate) =>
        candidate.id !== excluded &&
        (needle.length === 0 || candidate.name.toLowerCase().includes(needle)),
    );
  }, [candidates, filter, openSlot, bigId, littleId]);

  const pick = (candidate: Candidate) => {
    if (openSlot === "little") setLittleId(candidate.id);
    if (openSlot === "big") setBigId(candidate.id);
    setOpenSlot(null);
    setFilter("");
    setError(null);
    setConflictFromServer(false);
  };

  const canSubmit = littleId !== null && bigId !== null && littleId !== bigId && !alreadyPaired && !saving;

  const submit = async () => {
    if (!canSubmit || !littleId || !bigId) return;
    setSaving(true);
    setError(null);
    try {
      const edge = await createEdge(chapterId, {
        big_user_id: bigId,
        little_user_id: littleId,
        family_id: familyId,
        replace_existing: isReassign,
      });
      onSaved(edge);
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Someone else paired this little while the sheet was open. Do NOT
        // auto-replace what we cannot show — surface the conflict as the
        // reassignment state and let the historian decide with eyes open.
        setConflictFromServer(true);
        setError(null);
      } else if (err instanceof ApiError && err.detail === "lineage_cycle") {
        setError("That pairing would loop the tree. A big can't also be their little's descendant.");
      } else {
        setError(apiErrorMessage(err));
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      {/* Backdrop: onPress only, no button role — same c131 fix as CreateSheet. */}
      <Pressable
        onPress={onClose}
        style={{ flex: 1, backgroundColor: withAlpha(light.ink, 0.4), justifyContent: "flex-end" }}
      >
        <Pressable
          style={{
            backgroundColor: palette.surface,
            borderTopLeftRadius: radii.card,
            borderTopRightRadius: radii.card,
            paddingHorizontal: spacing.gutter,
            paddingTop: spacing.lg,
            paddingBottom: insets.bottom + spacing.lg,
            gap: spacing.lg,
            maxHeight: "88%",
          }}
        >
          <View
            style={{
              alignSelf: "center",
              width: 40,
              height: 4,
              borderRadius: radii.pill,
              backgroundColor: palette.border,
            }}
          />

          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Close"
            onPress={onClose}
            hitSlop={spacing.sm}
            style={{
              position: "absolute",
              top: spacing.lg,
              right: spacing.gutter,
              width: 28,
              height: 28,
              borderRadius: radii.pill,
              backgroundColor: palette.surfaceAlt,
              alignItems: "center",
              justifyContent: "center",
              zIndex: 1,
            }}
          >
            <Feather name="x" size={16} color={palette.inkSecondary} />
          </Pressable>

          <AppText variant="title">{isReassign ? "Reassign a big" : "Pair a big and little"}</AppText>

          <ScrollView style={{ flexGrow: 0 }} keyboardShouldPersistTaps="handled">
            <View style={{ gap: spacing.lg }}>
              <SlotField
                label="LITTLE"
                candidate={little}
                open={openSlot === "little"}
                onPress={() => {
                  setOpenSlot(openSlot === "little" ? null : "little");
                  setFilter("");
                }}
              />
              <SlotField
                label="BIG"
                candidate={big}
                open={openSlot === "big"}
                onPress={() => {
                  setOpenSlot(openSlot === "big" ? null : "big");
                  setFilter("");
                }}
              />

              {openSlot !== null ? (
                <View
                  style={{
                    borderRadius: radii.input,
                    borderWidth: 1,
                    borderColor: palette.border,
                    overflow: "hidden",
                  }}
                >
                  <TextInput
                    value={filter}
                    onChangeText={setFilter}
                    placeholder="Search the roster"
                    placeholderTextColor={palette.inkFaint}
                    style={{
                      ...typography.body,
                      color: palette.ink,
                      backgroundColor: palette.surfaceAlt,
                      paddingHorizontal: spacing.lg,
                      paddingVertical: spacing.md,
                    }}
                  />
                  <ScrollView style={{ maxHeight: 208 }} keyboardShouldPersistTaps="handled">
                    {filtered.length === 0 ? (
                      <AppText
                        variant="caption"
                        tone="tertiary"
                        style={{ padding: spacing.lg }}
                      >
                        Nobody matches that.
                      </AppText>
                    ) : (
                      filtered.map((candidate) => (
                        <Pressable
                          key={candidate.id}
                          accessibilityRole="button"
                          onPress={() => pick(candidate)}
                          style={({ pressed }) => ({
                            flexDirection: "row",
                            alignItems: "center",
                            gap: spacing.md,
                            paddingHorizontal: spacing.lg,
                            paddingVertical: spacing.sm,
                            backgroundColor: pressed ? palette.surfaceAlt : "transparent",
                          })}
                        >
                          <GradientAvatar
                            name={candidate.name}
                            size={32}
                            photoUrl={candidate.avatarUrl ?? undefined}
                          />
                          <AppText variant="body" style={{ flex: 1 }}>
                            {candidate.name}
                          </AppText>
                          {candidate.isGhost ? <Chip label="Historical" variant="neutral" /> : null}
                        </Pressable>
                      ))
                    )}
                  </ScrollView>
                </View>
              ) : null}

              {tree !== null && tree.families.length > 0 ? (
                <View style={{ gap: spacing.sm }}>
                  <AppText variant="micro" tone="secondary">
                    FAMILY
                  </AppText>
                  <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>
                    {tree.families.map((family) => {
                      const selected = familyId === family.id;
                      return (
                        <Pressable
                          key={family.id}
                          accessibilityRole="button"
                          accessibilityState={{ selected }}
                          onPress={() => setFamilyId(selected ? null : family.id)}
                          style={({ pressed }) => ({
                            flexDirection: "row",
                            alignItems: "center",
                            gap: spacing.xs,
                            paddingHorizontal: spacing.md,
                            paddingVertical: spacing.xs,
                            borderRadius: radii.pill,
                            backgroundColor: selected ? palette.accentSoft : palette.surfaceAlt,
                            borderWidth: 1,
                            borderColor: selected ? palette.accent : "transparent",
                            opacity: pressed ? 0.8 : 1,
                          })}
                        >
                          <View
                            style={{
                              width: spacing.sm,
                              height: spacing.sm,
                              borderRadius: radii.pill,
                              backgroundColor: family.color,
                            }}
                          />
                          <AppText
                            variant="caption"
                            tone={selected ? "accent" : "secondary"}
                          >
                            {family.name}
                          </AppText>
                        </Pressable>
                      );
                    })}
                  </View>
                </View>
              ) : null}

              {isReassign && little ? (
                <View
                  style={{
                    gap: spacing.xs,
                    padding: spacing.md,
                    borderRadius: radii.input,
                    backgroundColor: palette.warningSoft,
                  }}
                >
                  <View style={{ flexDirection: "row", alignItems: "center", gap: spacing.xs }}>
                    <Feather name="refresh-cw" size={13} color={palette.warning} />
                    <AppText variant="micro" style={{ color: palette.warning }}>
                      REASSIGNMENT
                    </AppText>
                  </View>
                  <AppText variant="caption" tone="secondary">
                    {currentBigName
                      ? `${little.name} already has a big on record: ${currentBigName}. `
                      : `${little.name} already has a big on record. `}
                    Saving replaces that pairing
                    {big ? `, and ${little.name} will be asked to confirm ${big.name} instead` : ""}.
                    The old confirmation does not carry over.
                  </AppText>
                </View>
              ) : null}

              {alreadyPaired && little && big ? (
                <AppText variant="caption" tone="tertiary">
                  {little.name} and {big.name} are already paired.
                </AppText>
              ) : null}

              {error !== null ? (
                <AppText variant="caption" tone="danger">
                  {error}
                </AppText>
              ) : null}
            </View>
          </ScrollView>

          <Button
            label={saving ? "Saving..." : isReassign ? "Reassign big" : "Pair them"}
            onPress={() => void submit()}
            disabled={!canSubmit}
          />
        </Pressable>
      </Pressable>
    </Modal>
  );
}

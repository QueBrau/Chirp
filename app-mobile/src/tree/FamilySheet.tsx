/**
 * FamilySheet (c79): create a family — name plus the color the tree canvas will
 * render for every node and branch in it. Swatches are the five validated
 * categorical slots from DESIGN §11 (palette.chartCategorical): family color is
 * stored data rendered for everyone in both modes, and those five are the only
 * palette in the design system built for adjacent-identity separation (CVD
 * checked). Taken swatches stay pickable but say who has them — steering, not
 * blocking, since a sixth family has to reuse something.
 */

import { Feather } from "@expo/vector-icons";
import { useEffect, useMemo, useState } from "react";
import { Modal, Pressable, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { ApiError } from "@/api/client";
import { createFamily, type FamilyOut } from "@/api/lineage";
import { AppText, Button } from "@/components";
import { light, radii, spacing, typography, useTheme, withAlpha } from "@/theme";

export interface FamilySheetProps {
  visible: boolean;
  onClose: () => void;
  chapterId: string;
  existing: FamilyOut[];
  onSaved: (family: FamilyOut) => void;
}

const SWATCHES: readonly string[] = light.chartCategorical;

export function FamilySheet({ visible, onClose, chapterId, existing, onSaved }: FamilySheetProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [name, setName] = useState("");
  const [color, setColor] = useState<string>(SWATCHES[0]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const takenBy = useMemo(() => {
    const map = new Map<string, string>();
    for (const family of existing) map.set(family.color.toLowerCase(), family.name);
    return map;
  }, [existing]);

  useEffect(() => {
    if (visible) {
      setName("");
      // Default to the first swatch no family is wearing yet.
      setColor(SWATCHES.find((swatch) => !takenBy.has(swatch.toLowerCase())) ?? SWATCHES[0]);
      setError(null);
    }
  }, [visible, takenBy]);

  const takenName = takenBy.get(color.toLowerCase()) ?? null;
  const canSubmit = name.trim().length > 0 && !saving;

  const submit = async () => {
    if (!canSubmit) return;
    setSaving(true);
    setError(null);
    try {
      const family = await createFamily(chapterId, { name: name.trim(), color });
      onSaved(family);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Something went wrong. Try again.");
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

          <AppText variant="title">New family</AppText>

          <View>
            <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
              NAME
            </AppText>
            <TextInput
              value={name}
              onChangeText={setName}
              placeholder="e.g. Hammer, Anchor, Compass"
              placeholderTextColor={palette.inkFaint}
              style={{
                ...typography.body,
                color: palette.ink,
                backgroundColor: palette.surfaceAlt,
                borderRadius: radii.input,
                paddingHorizontal: spacing.lg,
                paddingVertical: spacing.md,
              }}
            />
          </View>

          <View style={{ gap: spacing.sm }}>
            <AppText variant="micro" tone="secondary">
              TREE COLOR
            </AppText>
            <View style={{ flexDirection: "row", gap: spacing.md }}>
              {SWATCHES.map((swatch) => {
                const selected = swatch === color;
                return (
                  <Pressable
                    key={swatch}
                    accessibilityRole="button"
                    accessibilityState={{ selected }}
                    accessibilityLabel={`Family color ${swatch}`}
                    onPress={() => setColor(swatch)}
                    style={({ pressed }) => ({
                      width: 40,
                      height: 40,
                      borderRadius: radii.pill,
                      backgroundColor: swatch,
                      alignItems: "center",
                      justifyContent: "center",
                      borderWidth: selected ? 2 : 0,
                      borderColor: palette.ink,
                      opacity: pressed ? 0.8 : 1,
                    })}
                  >
                    {selected ? <Feather name="check" size={18} color={light.surface} /> : null}
                  </Pressable>
                );
              })}
            </View>
            {takenName !== null ? (
              <AppText variant="caption" tone="tertiary">
                {takenName} already wears this color, and two families sharing it will be hard to
                tell apart on the tree.
              </AppText>
            ) : (
              <AppText variant="caption" tone="tertiary">
                Every member and branch of this family renders in this color.
              </AppText>
            )}
          </View>

          {error !== null ? (
            <AppText variant="caption" tone="danger">
              {error}
            </AppText>
          ) : null}

          <Button label={saving ? "Creating..." : "Create family"} onPress={() => void submit()} disabled={!canSubmit} />
        </Pressable>
      </Pressable>
    </Modal>
  );
}

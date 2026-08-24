/**
 * Fab (DESIGN §7): Home-only floating create button — 56 accent circle,
 * Feather plus, floats 12 above the floating tab bar, bottom-right. Owns its
 * own open state and CreateSheet, so a screen just renders <Fab /> as an
 * absolutely-positioned overlay sibling (NOT inside the scrollable content,
 * so it stays fixed while the feed scrolls).
 */

import { Feather } from "@expo/vector-icons";
import { useState } from "react";
import { Pressable } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { cardShadow, metrics, radii, spacing, typography, useTheme } from "@/theme";

import { CreateSheet } from "./CreateSheet";

export interface FabProps {
  /** Chapter to post into, or null when the caller belongs to no chapter. */
  chapterId: string | null;
  /** The caller's campus — the create route for a chapter-less student (c71). */
  campusId: string | null;
  campusName?: string | null;
  onPosted?: () => void;
}

export function Fab({ chapterId, campusId, campusName, onPosted }: FabProps) {
  const palette = useTheme();
  const insets = useSafeAreaInsets();
  const [open, setOpen] = useState(false);

  const bottom = Math.max(insets.bottom, metrics.tabBarInsetBottom) + metrics.tabBarBoxHeight + spacing.md;

  // Hidden only when there is genuinely nowhere to post: no chapter AND no campus.
  // A chapter-less student keeps the button and posts to their campus (c71) — the
  // Aug 11 product line is that Chirp is for all students, so "no org" must not
  // mean "read only".
  if (chapterId === null && campusId === null) return null;

  return (
    <>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Create"
        onPress={() => setOpen(true)}
        style={({ pressed }) => ({
          position: "absolute",
          right: metrics.tabBarInsetX,
          bottom,
          width: metrics.fabSize,
          height: metrics.fabSize,
          borderRadius: radii.pill,
          backgroundColor: palette.accent,
          alignItems: "center",
          justifyContent: "center",
          opacity: pressed ? 0.85 : 1,
          ...cardShadow(palette),
        })}
      >
        <Feather name="plus" size={typography.title.fontSize} color={palette.onAccent} />
      </Pressable>
      <CreateSheet
        visible={open}
        onClose={() => setOpen(false)}
        chapterId={chapterId}
        campusId={campusId}
        campusName={campusName}
        onPosted={onPosted}
      />
    </>
  );
}

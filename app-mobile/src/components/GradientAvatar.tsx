/**
 * GradientAvatar per DESIGN.md §5: initials on a per-name gradient pair
 * (picked from the 5 preset pairs by name hash). Squircle radius 16.
 * Canonical sizes 32 / 40 / 48 (Profile hero uses 64).
 *
 * LinearGradient is not installed, so the gradient is simulated: solid start
 * color base + two offset translucent circles of the end color drifting toward
 * the bottom-right (reads as a 135deg blend).
 *
 * DESIGN §10.2: real imagery beats placeholder letters wherever a photo can
 * live. `photoUrl` renders in place of the fill (same radius/size, no layout
 * change) — the initials gradient is the fallback only, used when no photoUrl
 * is given OR the image fails to load.
 */

import { useState } from "react";
import { Image, View } from "react-native";

import { radii, useTheme, type GradientPair } from "@/theme";

import { AppText } from "./AppText";

export interface GradientAvatarProps {
  name: string;
  /** Canonical sizes: 32, 40, 48 (64 for the Profile hero). Default 40. */
  size?: number;
  /** Optional mock photo, e.g. `https://i.pravatar.cc/150?u=<id>` (§10.2). Falls back to the initials gradient when omitted or on load failure. */
  photoUrl?: string | null;
}

function initials(name: string): string {
  const words = name.replace(/["'.]/g, "").split(/\s+/).filter(Boolean);
  const first = words[0]?.[0] ?? "?";
  const last = words.length > 1 ? words[words.length - 1][0] : "";
  return `${first}${last}`.toUpperCase();
}

function hashName(name: string): number {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = (hash * 31 + name.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

export function GradientAvatar({ name, size = 40, photoUrl }: GradientAvatarProps) {
  const palette = useTheme();
  const [imageFailed, setImageFailed] = useState(false);
  const showPhoto = Boolean(photoUrl) && !imageFailed;

  const pairs = palette.avatarGradients;
  const pair: GradientPair = pairs[hashName(name) % pairs.length] ?? palette.accentGradient;
  const [start, end] = pair;

  return (
    <View
      style={{
        width: size,
        height: size,
        borderRadius: radii.avatar,
        backgroundColor: start,
        overflow: "hidden",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {showPhoto ? (
        <Image
          source={{ uri: photoUrl as string }}
          onError={() => setImageFailed(true)}
          style={{ width: size, height: size, borderRadius: radii.avatar }}
        />
      ) : (
        <>
          {/* Fake 135deg gradient: end color pooling toward the bottom-right. */}
          <View
            style={{
              position: "absolute",
              top: "25%",
              left: "25%",
              width: "120%",
              aspectRatio: 1,
              borderRadius: radii.pill,
              backgroundColor: end,
              opacity: 0.65,
            }}
          />
          <View
            style={{
              position: "absolute",
              top: "55%",
              left: "45%",
              width: "100%",
              aspectRatio: 1,
              borderRadius: radii.pill,
              backgroundColor: end,
              opacity: 0.8,
            }}
          />
          <AppText
            tone="onAccent"
            style={{ fontWeight: "700", fontSize: size * 0.35, lineHeight: size * 0.45 }}
          >
            {initials(name)}
          </AppText>
        </>
      )}
    </View>
  );
}

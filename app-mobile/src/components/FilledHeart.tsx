/**
 * FilledHeart (board c229): a Feather heart that is actually FILLED.
 *
 * c222 painted palette.like onto the liked heart and the heart stayed an outline.
 * That was not a colour bug. @expo/vector-icons' Feather is a STROKE-ONLY set with
 * no filled-heart glyph in it at all, so a `color` prop has nothing to reach except
 * the outline — the one thing every other app in this category fills the moment you
 * tap it, and the single clearest "did that register?" signal in the action row.
 *
 * Swapping in Ionicons/MaterialCommunityIcons, which do ship a filled heart, is out:
 * DESIGN.md ~153 is "Feather set only, never mixed icon families", and the optical
 * weight of another family's heart would clash with the message-circle and send
 * sitting 16px away from it. So this renders FEATHER'S OWN HEART, filled, as SVG —
 * no new icon family, no DESIGN.md amendment, identical silhouette.
 *
 * WHERE THE PATH CAME FROM, because it is not a shape anyone should re-type by hand:
 * it was extracted from the very font <Feather> renders,
 *   node_modules/@expo/vector-icons/build/vendor/react-native-vector-icons/
 *     Fonts/Feather.ttf                            (sha1 a6ca45c3…)
 * at the codepoint that package's own glyphmaps/Feather.json gives for "heart"
 * (0xf181), read with fontTools.
 *
 * The glyph has TWO contours, because a font cannot stroke: the generator expanded
 * Feather's 2px stroke into an outline, leaving an outer edge and an inner hole.
 * Filling both would just re-draw today's outline. What is below is the OUTER
 * contour alone, which is exactly the silhouette Feather already draws — so the
 * filled heart is the same size, the same shape, and carries the same stroke weight
 * as its outline twin, with no second `stroke` prop needed to fake it.
 *
 * Coordinates were converted from font units into Feather's 24x24 design box using
 * the font's OWN metrics rather than a guess: unitsPerEm 512, OS/2 sTypoAscender 448,
 * sTypoDescender -64 (448 - -64 = 512, i.e. the em box IS the design box), giving
 * x' = x * 24/512 and y' = (448 - y) * 24/512. That is why an <Svg> of width/height
 * `size` over viewBox "0 0 24 24" lands at precisely the size and position
 * <Feather name="heart" size={size} /> does, and the two can be swapped per-state
 * inside one layout slot without anything shifting.
 *
 * Deliberately NOT the path published on feathericons.com: that one is the 1px
 * centerline, which would need a stroke to look right and would then be a different
 * shape from what this app actually ships in its font.
 */

import Svg, { Path } from "react-native-svg";

/** Outer contour of Feather.ttf's `heart` glyph, in a 24x24 y-down box. See header. */
const FEATHER_HEART_SILHOUETTE =
  "M12.38 3.89L12 4.27L11.62 3.89Q9.75 2.02 7.05 2.02Q4.36 2.02 2.46 3.91Q0.56 5.81 0.56 8.51" +
  "Q0.56 11.2 2.44 13.08Q3.52 14.16 11.3 21.94Q11.58 22.22 12 22.22Q12.42 22.22 12.7 21.94" +
  "Q13.41 21.23 16.62 18.02Q19.83 14.81 21.56 13.08Q23.44 11.2 23.44 8.51Q23.44 5.81 21.54 3.91" +
  "Q19.64 2.02 16.95 2.02Q14.25 2.02 12.38 3.89Z";

export interface FilledHeartProps {
  /** Same number you would pass Feather's `size` — the two render at identical scale. */
  size: number;
  /** Normally palette.like; taken as a prop so this stays a dumb icon like Feather is. */
  color: string;
}

export function FilledHeart({ size, color }: FilledHeartProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24">
      <Path d={FEATHER_HEART_SILHOUETTE} fill={color} />
    </Svg>
  );
}

/**
 * ChirpMark (board c434): the Chirp logo mark as react-native-svg, for the places the app
 * has to draw its own brand rather than ship a bitmap (the loading screen, today).
 *
 * WHERE THE GEOMETRY COMES FROM: the website favicon, web/public/img/favicon.svg
 * (viewBox 0 0 32 32). The 32x32 rounded tile (rx 10), the speech-bubble-and-beak mark
 * and the eye circle are copied from it verbatim, so the app and the site cannot drift
 * into two slightly different birds. If the favicon's paths change, change them here.
 *
 * SOLID, NOT THE WEB'S GRADIENT, ON PURPOSE. The favicon fills its tile with an
 * indigo-to-violet gradient and the mark with white. Here the tile (when there is one)
 * and the mark are each ONE flat fill, passed in by the caller. A single flat color keeps
 * the mark legible on any campus color the loading screen sits on: a gradient tile would
 * be a second hue fighting a campus primary it was never chosen against.
 *
 * TWO SHAPES, picked by whether `tileColor` is given:
 *   - with `tileColor`: the full 32x32 tile + mark + eye, square, `size` x `size`.
 *   - without it: the mark + eye alone, cropped tight (viewBox "4 6 24 21", the mark spans
 *     x 5..27, y 7..26) with the aspect ratio kept, so `size` is the WIDTH and the height
 *     is size * 21 / 24. Used full-bleed on a campus color, where a tile would just be a
 *     second rectangle behind the first.
 *
 * `eyeColor` is whatever sits behind the mark (the tile color, or the surface the bare
 * mark is drawn on), because the eye is a hole in the bird, not a color of its own.
 *
 * Decorative: no accessibility label on the Svg. The screen that draws it labels itself.
 */

import Svg, { Circle, Path, Rect } from "react-native-svg";

/** The mark's outline in the favicon's 32x32 box. See header. */
const MARK_PATH = "M5 19.5C5 12.6 10.6 7 17.5 7H27v5.4A13.6 13.6 0 0 1 13.4 26H5v-6.5Z";

/** Tight crop of the mark and eye alone: x 4..28, y 6..27 (24 wide, 21 tall). */
const CROP_VIEW_BOX = "4 6 24 21";
const CROP_WIDTH = 24;
const CROP_HEIGHT = 21;

export interface ChirpMarkProps {
  /** Rendered width in points. Square with a tile; height is size * 21 / 24 without one. */
  size: number;
  /** Fill of the mark itself. */
  color: string;
  /** Fill of the eye: the color showing through the mark (the tile, or the surface behind). */
  eyeColor: string;
  /** When set, draw the full rounded tile in this color behind the mark. */
  tileColor?: string;
}

export function ChirpMark({ size, color, eyeColor, tileColor }: ChirpMarkProps) {
  const withTile = tileColor !== undefined;
  return (
    <Svg
      width={size}
      height={withTile ? size : (size * CROP_HEIGHT) / CROP_WIDTH}
      viewBox={withTile ? "0 0 32 32" : CROP_VIEW_BOX}
    >
      {withTile ? <Rect width={32} height={32} rx={10} fill={tileColor} /> : null}
      <Path d={MARK_PATH} fill={color} />
      <Circle cx={21} cy={13.2} r={2} fill={eyeColor} />
    </Svg>
  );
}

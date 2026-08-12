# Chirp — Design Tokens

> **Provenance note:** the dribbble design-recon step has NOT run. This is an authored
> default system — tasteful, neutral, and fully implementable today. When design recon
> produces real references, update this file first, then `src/theme/` to match. Screens
> must never hardcode values; everything visual flows from `src/theme` tokens
> (see CONVENTIONS.md).

## Brand

- **Brand color:** Chirp Indigo `#6366F1` — confident, collegiate without being
  team-colored, and matches the SPEC default family color for lineage trees.
- Voice: clean surfaces, generous whitespace, one accent used sparingly (primary
  actions, active tab, links). Danger/success reserved for semantics only.

## Color palettes

Both palettes ship; the app follows the system scheme by default (`useTheme()`).
Token names below are the exact keys of `Palette` in `src/theme/colors.ts`.

| Token | Light | Dark | Use |
|---|---|---|---|
| `bg` | `#FAFAFC` | `#0F0F14` | Screen background |
| `surface` | `#FFFFFF` | `#1A1A22` | Cards, sheets, list rows |
| `surfaceRaised` | `#FFFFFF` | `#23232D` | Elevated surfaces (modals, menus); pair with elevation |
| `textPrimary` | `#17171C` | `#F2F2F6` | Headings, primary copy |
| `textSecondary` | `#55555E` | `#B4B4C0` | Supporting copy, labels |
| `textTertiary` | `#8A8A94` | `#7C7C88` | Timestamps, placeholders, disabled |
| `border` | `#E4E4EA` | `#2E2E3A` | Hairlines, dividers, input borders |
| `accent` | `#6366F1` | `#818CF8` | Primary buttons, active tab, links |
| `accentMuted` | `#EEEFFE` | `#26284A` | Accent tint fills (badges, selected rows) |
| `onAccent` | `#FFFFFF` | `#0F0F14` | Text/icons on accent fills |
| `danger` | `#DC2626` | `#F87171` | Destructive actions, errors |
| `success` | `#16A34A` | `#4ADE80` | Paid/confirmed/present states |

## Spacing scale (4-based)

`xs 4 · sm 8 · md 12 · lg 16 · xl 24 · xxl 32 · xxxl 48`

Screen horizontal padding: `lg` (16). Gap between stacked cards: `md` (12).
Inside-card padding: `lg` (16).

## Type scale

System font (SF Pro / Roboto). Sizes are `fontSize / lineHeight`, weights are RN string weights.

| Variant | Size | Weight | Use |
|---|---|---|---|
| `display` | 28 / 34 | `700` | Screen titles, empty-state headlines |
| `title` | 20 / 26 | `600` | Card titles, section headers |
| `body` | 16 / 22 | `400` | Default copy, list rows, inputs |
| `caption` | 13 / 18 | `400` | Timestamps, helper text, badges |

## Radii

`sm 6 · md 10 · lg 16 · pill 999`

Inputs and badges: `sm`–`md`. Cards and sheets: `lg`. Avatars and pill buttons: `pill`.

## Elevation

Three levels, exported as `elevation` from `src/theme` (RN shadow + Android elevation):

- `none` — flat; rely on `border` hairlines for separation.
- `low` — cards on `bg`: shadowOpacity 0.06, radius 4, offset (0, 2), elevation 2.
- `medium` — modals/menus on `surfaceRaised`: shadowOpacity 0.10, radius 12, offset (0, 4), elevation 6.

In dark mode, prefer surface-color contrast (`surface` vs `surfaceRaised`) over heavier shadows.

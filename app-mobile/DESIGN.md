# Chirp Design System v2 — "Campus Modern"

Source: dribbble recon (social-app-mobile-ui, student-campus-app searches, Aug 2026)
+ product reframe: **Chirp is for all students**; greek chapters are orgs you join.
This file is the binding contract. Screens/components use ONLY these tokens via
`src/theme` — zero hardcoded hex/px in screens. If a value isn't here, add it here first.

## 1. Personality

Modern, clean, a little playful. Soft neutral canvas, white cards, ONE confident
violet-indigo accent used sparingly + gradient moments for hero surfaces. Generous
whitespace, bold tight headings, pill/capsule shapes everywhere. Never corporate,
never cluttered. Anonymous (Yak) content gets playful pastel treatment.

## 2. Color tokens

### Light (default)
| token | value | use |
|---|---|---|
| bg | #F6F7FB | screen canvas |
| surface | #FFFFFF | cards, sheets, tab bar |
| surfaceAlt | #EFF1F7 | inset wells, input bg, inactive pills |
| ink | #101223 | primary text |
| inkSecondary | #575B75 | secondary text, captions |
| inkFaint | #9BA0B8 | placeholders, timestamps |
| border | rgba(16,18,35,0.08) | hairline card borders, dividers |
| accent | #5B5BF6 | primary actions, active tab, links |
| accentSoft | #ECECFE | accent-tinted chip/pill backgrounds |
| accentGradient | #6366F1 → #8B5CF6 (135deg) | hero cards, avatars, brand moments |
| success | #17A673 | money in, confirmations |
| successSoft | #E3F6EE | success chip bg |
| danger | #E5484D | money out, destructive, downvote active |
| dangerSoft | #FDECEC | danger chip bg |
| warning | #F5A623 | pending states |
| yakTints | #FFF3E9 / #EDF6FF / #F3EDFF / #EAF8F1 | rotating card tints on Yak (by index % 4) |

### Dark
| token | value |
|---|---|
| bg | #0C0D14 |
| surface | #15161F |
| surfaceAlt | #1D1E2A |
| ink | #F2F3FA |
| inkSecondary | #A6AAC4 |
| inkFaint | #666B85 |
| border | rgba(242,243,250,0.09) |
| accent | #7C7CFF |
| accentSoft | rgba(124,124,255,0.16) |
| accentGradient | #6366F1 → #8B5CF6 |
| success/danger/warning | #2BD597 / #FF6369 / #FFB84D (softs = 16% alpha of each) |
| yakTints | 12% alpha versions of light tints |

Dark follows system (`useColorScheme`). Both palettes complete — no color defined
in only one mode.

## 3. Type scale (system font)

| token | size/weight/spacing | use |
|---|---|---|
| display | 30 / 800 / -0.5 | screen titles ("Feed") |
| title | 20 / 700 / -0.3 | card headlines, modal titles |
| headline | 16 / 700 | list row titles, post authors |
| body | 15 / 400 / lineHeight 21 | post bodies, descriptions |
| bodyBold | 15 / 600 | emphasized body |
| caption | 12.5 / 500 | timestamps, metadata |
| micro | 11 / 600 / +0.4 uppercase | chip labels, section eyebrows |
| stat | 17 / 800 tabular | scores, money amounts |

Money always tabular-nums. Screen titles pair with a `caption` subtitle in inkSecondary.

## 4. Shape & space

- Spacing scale (4-base): 4 / 8 / 12 / 16 / 20 / 24 / 32. Screen gutter = 20.
- Radii: card 20, pill 999, input 14, avatar 16 (squircle feel), thumbnail 12.
- Cards: surface bg + 1px border token + shadow `0 2px 16px rgba(16,18,35,0.06)`
  (dark mode: no shadow, border only). Never heavy drop shadows.
- Vertical rhythm: 12 between cards, 24 between sections, 8 title→content.

## 5. Signature components

- **GradientAvatar** — initials on a per-user gradient (pick pair from 5 preset
  gradients by name hash). Radius 16. Sizes 32/40/48.
- **Chip** — pill, micro type. Variants: neutral (surfaceAlt/inkSecondary),
  accent (accentSoft/accent), success, danger, warning. Used for roles
  ("President"), pledge classes, categories, "Correction" badges.
- **VotePill** — single vertical capsule on Yak cards: ▲ / score (stat type) / ▼ in
  surfaceAlt; active direction fills accent (up) / danger (down) with white glyph.
- **HeroCard** — accentGradient bg, white text, radius 20. For org identity header
  and treasurer balance. Max one per screen.
- **Floating tab bar** — surface pill container, radius 28, inset 12 horizontal /
  8 bottom, border + shadow per card spec. Active tab: accentSoft pill behind
  icon+label in accent; inactive: inkFaint icon only. 5 tabs: Home, Yak, Messages,
  Orgs, Profile.
- **Screen header** — display title + caption subtitle, no nav chrome, 24 top pad.
- **EmptyState** — small geometric mark (outlined circle or squircle drawn with
  Views in accentSoft/accent — NEVER an emoji), headline, one-line caption,
  optional accent Button. Friendly, never blank screens.
- **ListRow / Button / Card** — per tokens above; Button variants: primary (accent
  bg, white, pill, 52 tall), secondary (accentSoft/accent), ghost (transparent/
  inkSecondary), destructive (dangerSoft/danger).

## 6. Product reframe (copy + structure)

- Chirp serves ALL students. Never assume the user is greek.
- Tab 4 is **Orgs** (route dir stays `chapter/` for backend parity; the LABEL is
  Orgs). Two states:
  - **Member**: HeroCard with org name + your role Chip, then tool grid — Family
    Tree, Members (all members), Treasurer, Secretary (last two only for those
    roles/president). This is the current mock state (president of Sigma Chi).
  - **Non-member (design present in code behind mock flag)**: "Find your org" —
    invite code input + browsable category chips (Fraternities, Sororities, Clubs,
    Intramurals) with EmptyState. Greek registration is opt-in here.
- Feed tab is titled **Home** — subtitle shows the source ("Sigma Chi · Epsilon Mu"
  for members; "Lakeview State" later for campus-wide).
- Auth `account-type` copy: "I'm a student" / "I'm in a fraternity or sorority" /
  "I'm an alum" — same three backend account types, friendlier framing.
- Yak stays campus-wide + anonymous: NO avatars and NO masks of any kind — the
  yakTint card background + typography carry the anonymity. Optional small tinted
  geometric dot (8px circle in the card's tint, darkened) as the only marker.
  VotePill on the right.

## 7. Screen notes

- **Home = the FYP** (mix of Twitter / Instagram / Snapchat — reference: NexUX
  dribbble shot, Aug 12): anything is postable — text, photo, video. Structure top
  to bottom: Moments row (Snapchat DNA) → mixed-media feed (Insta/Twitter DNA) →
  floating create FAB.
  - **MomentsRow** (second reference: Mostafizur Rahaman dribbble shot — rounded
    story TILES, not circles): horizontal strip of 64x64 rounded-square tiles
    (radius 20) — GradientAvatar fill, 2px accent ring inset, name caption under;
    first tile = "Your story" (Feather plus in accentSoft). Mock-only taps.
  - **Feed filter pills** directly under the Moments row: horizontal pill
    segmented row — "For You" (active default) · "Campus" · "My Orgs". Active =
    accent bg white text; inactive = surfaceAlt inkSecondary. Mock: filters the
    post list by source.
  - **MediaPostCard** variants by post type:
    - *text*: current card style (avatar header, body, action row).
    - *photo*: full-bleed image (radius 20, height ~260), bottom scrim overlay
      (layered translucent ink Views, NOT a heavy black gradient) carrying author
      GradientAvatar + name + time in white, body caption below the media inside
      the card, action row.
    - *video*: same as photo + centered play button (Feather play in a
      surface-translucent 48 circle) + duration Chip top-right. Mock: static
      thumbnail, no playback.
  - Action row on ALL variants (ref-2 style): each action is a 36 circular
    surfaceAlt chip holding the Feather icon (heart / message-circle / send),
    with the count in a small attached Badge; active state = accentSoft chip +
    accent icon. Simple, tappable, modern — no bare icon rows.
  - **FAB**: 56 accent circle, Feather plus, bottom-right, floats 12 above the
    tab bar; opens a mock "Create" sheet (Photo / Video / Text options as
    ListRows with Feather icons). One FAB, Home only.
- **Icons everywhere**: @expo/vector-icons Feather set only (ships with Expo).
  Tab bar: home / radio / message-circle / grid / user. Never emoji, never
  mixed icon families.
- **Messages**: rows w/ GradientAvatar 48, headline name, caption preview
  (encrypted preview = "🔒 Message"), unread dot in accent. Thread: bubbles —
  mine = accent bg white text radius 20/6 corner, theirs = surfaceAlt ink.
  Composer pill input + accent circular send.
- **Treasurer**: HeroCard balance (stat 28), dues progress caption; ledger rows
  with +/- stat amounts in success/danger, Chip for corrections; section header
  "Append-only" caption.
- **Tree (placeholder)**: family Chips in family colors, indented big→little rows;
  keep placeholder note for milestone-6 Skia canvas.
- **Profile**: centered GradientAvatar 64, display name, Chips row (role, pledge
  class), then USER-ARRANGEABLE sections: profile content is a list of section
  cards (About, My Orgs, Activity, Alumni info, Settings) the user can reorder
  and show/hide via an "Edit layout" mode — pencil ghost-button by the header
  toggles it; in edit mode each section card gets up/down arrow buttons (Feather
  chevron-up/down) and an eye/eye-off visibility toggle. Order + visibility
  persist in local state (mock persistence for now; real per-user prefs later).
  No drag-drop dependency — arrows only.
- **Sign-in**: brand moment — accentGradient wordmark area, then Apple/Google/email
  buttons full-width pill, caption legal line.

## 8. Don'ts

## 8.5 Campus theming & user appearance (Aug 12 — Jose)

The app themes itself around THE USER'S SCHOOL COLORS, and the user controls it.

- Each campus carries `colors: { primary, secondary }` (mock: Lakeview State =
  forest `#0F6B3E` + gold `#E8A812`). Add to src/mocks campus data.
- **Accent source** (user choice): Campus primary (DEFAULT) / Campus secondary /
  Chirp violet. The chosen color becomes the `accent` token app-wide;
  `accentSoft` is derived (14-16% alpha). Gradient pairs shift to
  [accent, accent-lightened] when a campus color is active.
- **Background style** (user choice): System (default light/dark) / Campus tint —
  bg becomes a subtle wash of campus primary (light: ~6% tint over #F6F7FB;
  dark: ~10% tint over #0C0D14). Cards/surfaces stay neutral so content wins.
- Appearance screen: Profile → Settings → Appearance (`profile/appearance.tsx`) —
  section cards with swatch rows (tappable color circles w/ check icon) for
  accent source + background style, live preview card at top. Prefs in a theme
  context with mock persistence; every existing screen must react instantly
  (all styling already flows through useTheme()).
- Contrast rule: campus colors are used for accent/tint only — body text stays
  ink on neutral surfaces. If a campus primary is too light for white button
  text, darken it for the accent role (document the adjustment in code).

**No emojis. Anywhere.** Not in UI, not in copy, not in placeholder content, not
in icons — vector icons (Feather) or geometric Views only. Emoji reads as AI slop
(product decision, Aug 11). Also: no pure black #000 in light mode. No borders +
heavy shadow together at full strength. No more than one HeroCard per screen. No
accent-colored body text. No dense screens — if it feels full, split into cards.
No new colors outside §2.

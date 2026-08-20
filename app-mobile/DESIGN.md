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

- Each campus carries `colors: { primary, secondary }`. **Mock campus is now
  UNC GREENSBORO** (replaces Lakeview State everywhere in mocks/copy): Spartan
  navy `#0B2340` primary + Spartan gold `#FFB71B` secondary (approx brand values
  — swap for official hex when we get the guide). Default appearance prefs:
  accentSource `campusPrimary`, backgroundStyle `campusTint` — the app should
  FEEL like the school out of the box (Jose, Aug 12).
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

## 8.6 Greek org colors (Aug 12 — Jose)

Every fraternity and sorority has ITS OWN colors, and org-scoped UI wears them.

- Chapters in mocks carry `colors: { primary, secondary }` — Sigma Chi = blue
  `#1F4396` + old gold `#D6A756`; add at least one sorority with hers (e.g.
  Alpha Delta Pi azure `#2E9BD6` + navy) so both are proven.
- **OrgAccentScope**: a theme-scope component that overrides accent/accentSoft/
  gradient tokens for its subtree. The ENTIRE Orgs stack (chapter/* screens)
  renders inside the current org's scope — hero, tool tiles, chips, active
  states all in org colors. Campus colors everywhere else.
- **Org posts NEVER appear on the FYP** (Jose, Aug 12): org content lives only
  inside the org's own space (§8.7). The FYP filter pills become "For You" /
  "Campus" only — no "My Orgs" pill, no org stripes on public feed cards.
- Contrast guard applies to org colors exactly like campus colors.

## 8.7 Org space: private feed + events (Aug 12 — Jose)

The Orgs tab becomes the org's own world, in the org's colors (§8.6), with three
segments under the org hero — a pill segmented control: **Feed · Events · Tools**.

- **Org feed**: chapter-only posts ("stuff they share only with themselves") —
  same MediaPostCard system as the FYP but rendered inside OrgAccentScope, backed
  by the org's posts in mocks (backend already scopes posts per chapter, so this
  maps 1:1 to /chapters/{id}/posts later). Composer FAB here too (org-colored).
- **Events — the Partiful corner**: playful event planning inside the org.
  - Event card: full-bleed cover (picsum seeded), oversized event title, date
    Chip (org accent) + location caption, host row (avatar + "Hosted by ..."),
    RSVP summary = overlapping avatar stack + "23 going".
  - Event detail screen: cover hero, title/when/where block, RSVP pill row —
    Going / Maybe / Can't — selected state in org accent (gold moment allowed
    for the Going count), guest list grouped by RSVP, mock "Invite" button.
  - Create-event sheet (mock): title, date, location, cover choice — matches
    CreateSheet pattern.
  - Backed by mocks now; backend events/event_rsvps tables are a board card.
- **Tools**: the existing role-gated grid (Tree, Members, Treasurer, Secretary)
  moves under this segment unchanged.

## 10. Craft rules — the anti-slop pass (Aug 12 — Jose: "looks super lazy / AI slop")

Generic-clean is not enough. Every screen must pass these:

1. **Zones, not card soup.** Each screen has a distinct header zone and content
   zone. Home/Yak/Orgs headers get an oversized title with a short gold accent
   bar (4x28, radius 2) under it, plus a micro eyebrow above ("UNC GREENSBORO ·
   SPARTANS" style). Never an unbroken stack of identical white rectangles —
   vary card sizes, insets, and groupings.
2. **Real imagery.** People get photo avatars: `https://i.pravatar.cc/150?u=<id>`
   seeded per user (GradientAvatar gains an optional photo uri, initials become
   the fallback only). Story tiles show the photo. Media posts use picsum photos.
   Placeholder-letter UI reads as lazy — kill it wherever a photo can live.
3. **Density contrast.** Text posts are COMPACT (Twitter density: tight header
   row, body, inline counts). Media posts breathe (Insta density). Identical
   spacing everywhere is the slop tell.
4. **One gold moment per screen.** Spartan gold is the delight color: the accent
   bar, an active vote, the balance figure, an unread ring. Never gold-wash
   whole surfaces; never zero gold either.
5. **Yak is a PLACE, not a list.** The Yak board renders on a deep-navy campus
   canvas (campus primary dark wash over bg in BOTH light and dark modes) with
   light tinted cards floating on it and gold vote states. It should feel like
   the campus at night — instantly distinct from Home.
6. **Numbers have personality.** All counts/scores/money in the stat type,
   tabular; notable numbers (top yak score, balance) get gold.
7. **Copy is specific.** Mock content and microcopy name real things (UNCG,
   Spartans, College Ave, EUC) — never lorem-ipsum-flavored filler.

**No emojis. Anywhere.** Not in UI, not in copy, not in placeholder content, not
in icons — vector icons (Feather) or geometric Views only. Emoji reads as AI slop
(product decision, Aug 11). Also: no pure black #000 in light mode. No borders +
heavy shadow together at full strength. No more than one HeroCard per screen. No
accent-colored body text. No dense screens — if it feels full, split into cards.
No new colors outside §2.

## 11. Data visualisation (Aug 18) — charts are computed, not styled

Added for the treasurer dashboard (board c118). §2 had no chart tokens at all, and
this file's own rule is that a value gets added here before it gets used.

**The form follows the data's job, not the request.** "Add pie charts" is a request
for a form, and three of the four things a treasurer needs are not pies:

| The question | Form | Why not the obvious thing |
|---|---|---|
| What is the balance right now? | hero figure on the HeroCard | a one-bar chart is a number with decoration |
| How did it get there? | line + area, ONE series | a multi-colour chart implies series that don't exist |
| How much of dues is in? | **meter** | a 2-slice pie makes you compare two angles to read one percentage |
| What did we spend it on? | **donut**, <= 5 + Other | this one genuinely is part-to-whole |

A donut is legal for part-to-whole **at a glance only**, never for comparing close
values — so every slice's exact amount sits in the legend beside it. Past ~6 segments
adjacent slices stop being separable at any palette, so the tail folds into one
"Other" rather than growing more colours.

### Categorical palette (`palette.chartCategorical`)

Five slots, fixed order, assigned in sequence and **never cycled**. The order IS the
colour-blindness safety mechanism, not a preference.

| mode | slots |
|---|---|
| light (on `surface` #FFFFFF) | `#5B5BF6` `#DB2777` `#0284C7` `#EA580C` `#0D9488` |
| dark (on `surface` #15161F) | `#7C7CFF` `#EC4899` `#0891B2` `#EA580C` `#10A99A` |

Both were **run through a validator, not eyeballed** — lightness band, chroma floor,
CVD separation under protanopia/deuteranopia, a normal-vision floor and contrast vs
the surface. Light clears at worst-adjacent 13.8 CVD / 28.8 normal; dark at 8.1 /
24.4; all ten swatches >= 3:1 on their surface. The donut's **wrap-around pair** was
checked too, since the last segment touches the first. The slot order came out of
enumerating all 120 permutations and taking the one with the best worst-case adjacent
separation. **Do not hand-edit a slot — re-run the validator.**

Dark is its own set of steps, not a flip: the light steps sit outside the dark
lightness band entirely.

- **`chartOther`** is a neutral, deliberately not a sixth hue. "Other" is the absence
  of an identity; giving it one implies a category that isn't there.
- **Colour follows the entity, never its rank.** Slices display largest-first, but the
  colour comes from a stable slot derived from the category's own label
  (`assignCategorySlots`). Two categories swapping places must not swap colours.
- **Status colours are reserved.** `success`/`danger` mean money in and money out. A
  spend category wearing them would be making a claim about the money that isn't true,
  so they are never reused as series identity.
- **Categorical slots do NOT follow the org accent** (§8.6), because they are validated
  values. Single-series charts DO use `accent`, so a trend line wears the org's colour
  automatically.

### Marks

- Line **2px**, round join and cap. Area fill = the same hue at **10% opacity**, a
  wash and never a saturated block.
- End dot r=4.5 with a **2px ring in the surface colour** — a ring, not a border, so it
  stays legible where it crosses its own line.
- **2px of surface between touching marks** (donut segments), specified in pixels and
  converted to degrees at the ring's mid radius, so the gap looks even at any size.
- Gridlines/axis rules: hairline 1px solid in `border`, recessive. The zero line is
  drawn **only when the series actually crosses zero**.
- Meter track is `accentSoft` — a lighter step of the fill's own ramp, not a neutral —
  so state reads across the whole bar.

### Rules that are easy to get wrong

- **Text never wears the data colour.** Labels, values and legends use ink tokens; the
  swatch beside them carries identity. A light hue is illegible as text on `surface`.
- **A legend is always present for two or more series**; a single series gets none, as
  the card title already names it.
- **No gold on any chart mark.** Spartan gold is 1.74:1 on white — a gold data mark is
  nearly invisible in light mode. The screen's one gold moment (§10.4) is decorative
  and lives on the violet hero, where it has a dark ground behind it.
- **The area closes onto zero when zero is in range**, not onto the bottom of the box:
  an overdrawn chapter's deficit must not shade like a surplus.

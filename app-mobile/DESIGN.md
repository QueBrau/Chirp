# Chirp Design System v2 — "Campus Modern"

Source: dribbble recon (social-app-mobile-ui, student-campus-app searches, Aug 2026)
+ product reframe: **Chirp is for all students**; greek chapters are orgs you join.
This file is the binding contract. Screens/components use ONLY these tokens via
`src/theme` — zero hardcoded hex/px in screens. If a value isn't here, add it here first.

## 1. Personality

Modern, clean, a little playful. Soft neutral canvas, white cards, ONE confident
violet-indigo accent used sparingly + gradient moments for hero surfaces. Generous
whitespace, bold tight headings, pill/capsule shapes everywhere. Never corporate,
never cluttered. Anonymous (Chirps) content gets playful pastel treatment.

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
| like | #E5484D | a liked post's heart. Its OWN token, not `danger` (board c222) |
| dangerSoft | #FDECEC | danger chip bg |
| warning | #F5A623 | pending states |

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

**`chirpTints` is RETIRED (braul, Sep 10, c384). Post cards are `surface` in both
modes - white in light, #15161F in dark - and nothing rotates.**

The token is recorded here rather than simply deleted because the reasoning is the
useful part. It shipped as four near-white pastels rotated by card index, and c383 gave
every post card one. Dark was defined as 12% alpha versions of the same four, which
composited over `bg` to #29292E / #272930 / #282830 / #27292F - a worst adjacent
euclidean distance of **1.3/255**, i.e. no difference at all. That was inherent to the
definition, not a bad choice of colour: all four light tints are near-white, so their
differences are small in absolute terms and 12% alpha shrinks them a further 8x. A
measured dark replacement (each light tint's own hue at S 0.20, L 0.115, worst adjacent
7.0) was put to braul as one of three options.

He took none of them and went further: no colour on post cards in EITHER mode. So the
question "what should the dark tints be" is closed by there being no tints. What breaks
up a scrolling column now is the card's own edge, shadow and spacing, plus density
contrast (§10.3) - which §10.1 must therefore keep asking for, in both modes.

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
- Radii: card 20, pill 999, input 14, avatar 16 (squircle feel), thumbnail 12,
  media 16 (a post's inset photo/video block, §5 PostCard). Media is its own
  token rather than borrowing `avatar`: they happen to share a number today, and a
  later squircle tweak to avatars must not silently reshape every photo in the feed.
- Cards: surface bg + 1px border token + shadow `0 2px 16px rgba(16,18,35,0.06)`
  (dark mode: no shadow, border only). Never heavy drop shadows.
- Vertical rhythm: 12 between cards, 24 between sections, 8 title→content.

## 5. Signature components

- **GradientAvatar** — initials on a per-user gradient (pick pair from 5 preset
  gradients by name hash). Radius 16. Sizes 32/40/48.
- **Chip** — pill, micro type. Variants: neutral (surfaceAlt/inkSecondary),
  accent (accentSoft/accent), success, danger, warning. Used for roles
  ("President"), pledge classes, categories, "Correction" badges.
- **VotePill** — single vertical capsule on Chirps cards: ▲ / score (stat type) / ▼ in
  surfaceAlt; active direction fills accent (up) / danger (down) with white glyph.
- **HeroCard** — accentGradient bg, white text, radius 20. For org identity header
  and treasurer balance. Max one per screen.
- **Floating tab bar** — surface pill container, radius 28, inset 12 horizontal /
  8 bottom, border + shadow per card spec. Active tab: SOLID accent pill behind
  icon+label in the CAMPUS SECONDARY (the gold moment token, read from
  `useAppearance().campusColors`, never a hardcoded hex); inactive: inkSecondary
  icon only. 5 tabs: Home, Chirps, Messages, Orgs, Profile.
  - Was accentSoft-pill + accent icon + inkFaint inactive until Sep 4 (braul,
    board c310). Two reasons it changed, and both constrain any future edit.
    (1) The inactive icon was inkFaint — the faintest ink token in the system,
    on a floating bar over live content. That was the real legibility complaint.
    (2) The pill MUST stay solid if the icon is gold: campus secondary on the
    pale accentSoft pill is LOWER contrast than the navy it replaced, so
    recolouring the icon alone makes the bar worse, not better. Solid
    accent + gold is also UNCG's own pairing.
  - **This is a deliberate exception to §10.4 rule 4** ("one gold moment per
    screen"). The bar is persistent chrome, so gold now appears on every screen
    alongside that screen's own moment — Home and Chirps both spend theirs on
    the header accent bar. Taken knowingly at braul's request; if it reads as
    too much gold, the cheap revert is gold on the icon with the label left on
    a tone token.
- **PostCard** (c383, flattened by c384) — the one card every post wears, on Chirps,
  the FYP and the org feed. `surface` background, radius 20, hairline border, §4 card
  shadow. Structure top to bottom: header row (author identity on the left, a circular
  soft control on the right), body, optional inset media, action row.
  - It was a **TintedPostCard** until c384: a rotating `chirpTints[index % 4]`
    background, explicitly "never `surface`". braul retired the colour in both modes
    (§2). The name went with it, because a component called TintedPostCard is an
    invitation to put the tint back.
  - Breaking up a scrolling column is now the job of the card edge, the shadow and
    density contrast (§10.3) — in BOTH modes, not just dark. §10.1 no longer has a
    tint to delegate that to.
  - **Circular soft control** — the 32 circle behind an overflow glyph in the header
    row. `surfaceAlt` in both modes, from `onCardControl(palette)` in `src/theme`,
    next to `cardShadow`, so the modes cannot drift apart. It was `surface` in light
    (white on a pastel tint) and a 10% ink wash in dark; both halves died with the
    tints, and the light half became an active bug the moment the card underneath it
    also became `surface` — a white circle on a white card. `surfaceAlt` steps the
    correct direction in each mode without a mode switch, and matches the input
    background, so a control and a text field on one card finally agree.
- **Screen header** — display title + caption subtitle, no nav chrome, 24 top pad.
- **EmptyState** — small geometric mark (outlined circle or squircle drawn with
  Views in accentSoft/accent — NEVER an emoji), headline, one-line caption,
  optional accent Button. Friendly, never blank screens.
- **ListRow / Button / Card** — per tokens above; Button variants: primary (accent
  bg, white, pill, 52 tall), secondary (accentSoft/accent), ghost (transparent/
  inkSecondary), destructive (dangerSoft/danger), **brand** (accent bg, campus
  SECONDARY label — c385), **neutral** (surfaceAlt bg, ink label — c385).
  - `brand` is the gold-moment CTA, and it is a NAMED VARIANT rather than a
    `labelColor` prop on purpose: an arbitrary-colour escape hatch on the one
    shared button is how a design system stops being one. It reads campus
    secondary from `useAppearance()` itself, so there is no colour to pass and
    none to get wrong. Solid accent + campus secondary is the SAME pairing the
    floating tab bar already ships (§5, c310) and is UNCG's own; gold on navy
    measures ~8.6:1 in both modes. One per screen (§10.4 rule 4).
  - `neutral` is the quiet filled button, with no accent in it at all. It exists
    because **`secondary` is unreadable in dark mode whenever the accent is dark**:
    the default accent source is campus primary, and UNCG's navy label on the
    accentSoft fill measures **1.18:1** in dark against 10.96:1 in light, so the
    defect is dark-only and app-wide (board c386). surfaceAlt + ink is ~15:1 in
    both modes by construction. Use it when a filled button should not be an accent
    moment.
- **UnderlineField** (c385) — the auth screens' text field: `caption`/secondary
  label above, a value row in `body`, a 1px bottom border in `border` that becomes
  `accent` on focus, and an optional trailing icon button (the password eye
  toggle; the email `at-sign` is decorative and not a button).
  - It does **NOT** replace `inputField()`. That filled `surfaceAlt` treatment is
    what every other form in the app uses and stays the default; this one is
    scoped to auth, where the reference's open, line-only fields are the look.
    Two field treatments is a deliberate split, not drift — if a third appears,
    something has gone wrong.

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
- Chirps stays campus-wide + anonymous: NO avatars and NO masks of any kind — the
  chirpTint card background + typography carry the anonymity. Small tinted
  geometric dot (8px circle in the card's tint, darkened) beside the meta line.
  VotePill on the right.
- **A card carries a DAILY-ROTATING PSEUDONYM, not the word "anonymous"** (c390,
  braul, Sep 8 — "randomized name like in reddit and yikyak"). `author_label` comes
  from the server (`Quiet-Magnolia-07`); the client cannot compute or reverse it.
  - He was given three options with their costs and chose the middle one
    knowingly. A name that labels one POST costs nothing and tracks nobody. A
    Reddit-style name that is stable forever makes one identifying chirp
    deanonymize a person's whole history, which on a single campus is a real
    risk. The daily rotation caps linkage at a day.
  - **The dot stays.** It is what says "this is the anonymous board"; the label on
    its own reads like a username and would imply an account that does not exist.
  - No avatar, ever — a name is not permission to add a face. The §5
    GradientAvatar must not appear on this board.
  - The subtitle copy changed with it, because "No names, ever." became false. It
    now says the true thing: *"Campus-wide and anonymous. Your name here changes
    every day."* If the rotation is ever removed, that line has to change again.

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
  - **MediaPostCard** is a PostCard (§5). ONE structure for every post type,
    changed Sep 7 (braul, board c383) from a reference shot he supplied:
    - header row: GradientAvatar 40 + name (headline) over time (caption), with the
      circular overflow control at the right end. Any tier Chip ("Actives only")
      sits between them.
    - body text, then the media block (photo/video only), then the action row.
    - *photo*: image INSET inside the card padding at radius `media`, height ~240.
    - *video*: same block + centered play (Feather play in a translucent 48 circle)
      + duration Chip top-right OF THE MEDIA.
    - The author row is no longer floated over the photo on a translucent scrim, and
      the scrim is gone with it. Two things that cost us go with it: the pinned-light
      `onScrim` tone path through AuthorRow and OverflowButton, and the separate
      unavailable-media tone branch (c140) that existed only because a failed image
      left white scrim text on a pale surface. An unavailable photo is now just an
      inset `surfaceAlt` block with the image glyph and its caption.
  - Action row on ALL variants: Feather icon (heart / message-circle / send) beside
    a caption label, generously spaced, no chip circle. The label is **the count when
    there is one, and the action's name when there is not** — "Like" rather than a
    meaningless "0", and a real number the moment one exists (§10.6). The
    accessibility label is always the full action name, never the digits. Active like
    = FilledHeart in `like` with its count in the same red (c222/c229 unchanged: only
    an ACTIVE HEART ever becomes the filled shape, and an unliked post looks exactly
    as it always did).
    - Every action must clear a 44pt touch target (c307). These rows are icon+label,
      ~21pt tall, so they carry hitSlop derived from `TOUCH_TARGET` rather than a
      hand-picked number - the mistake c307 fixed was a hand-picked 8.
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
- **Sign-in / sign-up**: ONE page, not two stages (c385, braul, Sep 7, from a
  reference shot). Oversized `display` title + `caption` subtitle, then the email
  form, then the CTA, then the social row, then the footer mode toggle. The title
  IS the brand moment now: the accentGradient HeroCard wordmark it replaced was a
  block of chrome above a screen whose actual job — the form — was hidden behind a
  "Continue with Email" tap.
  - Fields are UnderlineFields (§5): E-mail (decorative `at-sign`), Password (eye
    toggle), and on sign-up only, Repeat password (eye toggle, real client-side
    match check before Firebase is called).
  - **Forgot password?** sits right-aligned above the CTA, sign-in mode only. It
    is a real `sendPasswordResetEmail` call, added with this card. It is absent on
    sign-up, where it means nothing, and the reference's own screen shows it there.
  - **No "Remember for 30 days" checkbox**, which the reference has. Sessions
    already persist INDEFINITELY (c166: AsyncStorage on native, browser
    persistence on web), so the control would promise less than the truth and
    unchecking it would have to do nothing. A checkbox that cannot change the
    behaviour it names is worse than no checkbox.
  - **Two social buttons, not three.** Chirp has Apple and Google; the reference's
    third (Instagram) is not a provider this app has, and sign-in.tsx's own rule is
    that a control which looks like authentication must never be one that isn't.
    They keep short TEXT labels rather than the reference's icon-only circles:
    Feather has no brand marks, mixing icon families is forbidden above, and a
    lettered circle is the placeholder-letter UI §10.2 calls lazy. Apple and Google
    both require their official marks for sign-in buttons, so icon-only is an asset
    job, not a styling one.
  - Caption legal line stays at the bottom.

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
   zone. Home/Chirp/Orgs headers get an oversized title with a short gold accent
   bar (4 wide, radius 2) **leading it on the left**, plus a micro eyebrow above
   ("UNC GREENSBORO · SPARTANS" style). Never an unbroken stack of identical
   white rectangles — vary card sizes, insets, and groupings.

   *Changed Aug 25 (braul): the bar used to sit UNDER the title at a fixed 28
   tall. It now leads the title and takes the title's own height via
   `alignSelf: "stretch"` rather than a fixed number, so it stays aligned if the
   display scale moves. `metrics.accentBarHeight` was deleted rather than left
   unused — kept around, the next hand-rolled header would have reached for it
   and quietly reintroduced a bar that no longer matches the text beside it.*
2. **Real imagery.** People get photo avatars: `https://i.pravatar.cc/150?u=<id>`
   seeded per user (GradientAvatar gains an optional photo uri, initials become
   the fallback only). Story tiles show the photo. Media posts use picsum photos.
   Placeholder-letter UI reads as lazy — kill it wherever a photo can live.
3. **Density contrast.** Text posts are COMPACT, media posts breathe. Identical
   spacing everywhere is the slop tell.

   *Narrowed Sep 7 (braul, board c383).* This used to mean two different CARDS: a
   tight one-line header and inline counts for text, a roomy stacked header and 36px
   action chips for media. The reference braul asked both feeds to resemble gives
   both post types the same header and the same action row, and differs only in
   whether a photo is present - so the chrome is now shared and the contrast lives
   in the padding and the gaps alone (text `lg`/`sm`, media `lg`/`md` with the media
   block itself doing the breathing).
   *Un-narrowed Sep 10 (braul, board c384).* Between c383 and c384 this rule said the
   anti-slop job had moved to the rotating tint - four alternating pastels breaking up
   a scrolling column more effectively than two spacing densities, because a tint works
   on a run of posts that are all the same type, which is what a real feed usually is.

   That was recorded at the time as TRUE IN LIGHT MODE ONLY, since the dark tints were
   measurably identical to one another, and it flagged the risk plainly: narrowing rule
   3 and then finding the replacement does not work in dark is the sequence that leaves
   a feed with neither device. braul resolved it by removing the tint in BOTH modes
   rather than fixing the dark set, so that risk is now the situation in both.

   **So density contrast is load-bearing again, in both modes, and is the only
   mechanism rule 1's "unbroken stack of identical rectangles" has.** The chrome stays
   shared - c383's reading of the reference was not overturned, and the header and
   action row remain the same for both post types - but the padding and gap difference
   (text `lg`/`sm`, media `lg`/`md`) is now doing that work alone and must not be
   narrowed further without a replacement that has been measured in dark as well as
   light. Card edge, shadow and spacing are what is left.
4. **One gold moment per screen.** Spartan gold is the delight color: the accent
   bar, an active vote, the balance figure, an unread ring. Never gold-wash
   whole surfaces; never zero gold either.
5. **Chirps is a PLACE, not a list.** The Chirps board renders on a deep-navy campus
   canvas (campus primary dark wash over bg) **in dark mode**, with gold vote states.
   It should feel like the campus at night — instantly distinct from Home. In **light
   mode** it uses the normal app canvas: the wash applied in both schemes until Aug 27
   (braul, board c219), which made Chirps the one screen ignoring the system setting
   and reading as an island. Whatever the canvas, the header tone must move with it:
   the board's header is hand-rolled and white-on-navy, so a canvas change without a
   tone change ships invisible text.

   *The cards used to be pinned. They are not any more (braul, Sep 10, board c384).*
   Until c384 the chirp cards were light-tinted in BOTH schemes, and everything drawn
   on them — ink, icons, the vote score — was pinned to the `light` palette, because a
   live-theme colour on a pinned card is what made the score vanish at night (c297).
   With the tints retired (§2), the cards are ordinary `surface` cards following the
   system scheme, and the pin is gone with them. **That is a stronger guarantee than
   the pin was:** there is no second palette left on this screen for the live one to
   disagree with, so the c297 pairing is now unreachable rather than merely prevented.
   `verify-chirps-board.mjs` asserts the un-pinning directly, because the way this
   comes back is someone reintroducing a pinned palette here.

   The obvious objection — a dark card will not separate from the navy wash — was
   measured before the change, not after: card #15161F on UNCG's wash #05101D is a
   euclidean distance of 17.2, against 16.8 for an ordinary dark card on the ordinary
   dark canvas and 12.7 in light mode. The flat card on navy separates no worse than
   every other card in the app already does, and the border and shadow carry the edge
   exactly as they do everywhere else.

   *Pressure on this rule, recorded Sep 7 (braul, board c383) rather than glossed.*
   The FYP and Chirps use the same card (§5 PostCard), so in LIGHT mode — where c219
   correctly gave Chirps the ordinary canvas — the two boards read as siblings rather
   than as different places. That was braul's explicit call, from a single reference
   shot for both screens, and c384 sharpened it by removing the tint that was the last
   decorative difference. What still separates them is everything that carries meaning
   rather than decoration: the navy canvas at night, no avatar and no name ever (§6,
   SPEC §8.3), the anonymity dot, the VotePill and gold vote states instead of
   like/comment, and the composer sitting at the top of the board. If it ever reads as
   too alike, the fix is a structural difference, NOT re-forcing the navy in light mode
   (the island c219 removed) and NOT a tint quartet for the FYP (the decoration c384
   removed).
6. **Numbers have personality.** All counts/scores/money in the stat type,
   tabular; notable numbers (top chirp score, balance) get gold.
7. **Copy is specific.** Mock content and microcopy name real things (UNCG,
   Spartans, College Ave, EUC) — never lorem-ipsum-flavored filler.

**No emojis. Anywhere.** Not in UI, not in copy, not in placeholder content, not
in icons — vector icons (Feather) or geometric Views only. Emoji reads as AI slop
(product decision, Aug 11).

**No em dashes in UI copy.** Not in labels, captions, empty states, alerts, or mock
content. An em dash reads as AI-written for the same reason emoji does (product
decision, braul, Aug 27, board c220). Use a full stop when the two halves are
independent clauses, a comma when the second half explains the first, and a colon
when it introduces a list. Do NOT reach for a semicolon as a substitute; it reads
the same way. This is about the DASH, not the hyphen: `e-board`, `campus-wide`,
`append-only` and `as-is` are ordinary compound words and stay. Code comments are
not UI and are out of scope.

Also: no pure black #000 in light mode. No borders + heavy shadow together at full
strength. No more than one HeroCard per screen. No accent-colored body text. No
dense screens — if it feels full, split into cards. No new colors outside §2.

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

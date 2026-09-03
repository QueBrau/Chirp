/** Leftover mock world. NOTE: the USE_MOCKS layer this once fed was DELETED in
 * PR #8 — src/api/* now always talks to the real backend, so nothing here is
 * served by the API layer any more. What still reads it: the Home Moments row
 * (no backend concept exists) and a few design constants (campus/org colors).
 * Treat any OTHER import of this module as a bug — it means a screen is
 * rendering fake data against a live app.
 *
 * UNC Greensboro → Sigma Chi, Epsilon Mu chapter. Current user is the president
 * (Jake Miller). A second org, Alpha Delta Pi (Zeta Rho), exists purely to prove
 * the org theming system is org-agnostic (DESIGN §8.6) — it isn't the signed-in
 * user's chapter. Everything here is MOCK DATA: message "plaintext" is included
 * only so screens can render — real messages are E2EE and their plaintext only
 * ever exists in the on-device SQLite store (SPEC §2.1, §6).
 *
 * Type-only imports from ../api/* keep this module cycle-free at runtime.
 */

import type { CampusOut, UserOut } from "../api/auth";
import { brand, DEFAULT_APPEARANCE_PREFS, type AppearancePrefs, type CampusColors } from "@/theme";
import type { ChapterInviteOut, ChapterOut, MembershipOut, RoleMetaOut } from "../api/chapters";
import type { DeviceOut, PrekeyBundleOut } from "../api/keys";
import type { ConversationOut, MessageOut } from "../api/messages";
import type { PostCommentOut, PostLikeOut, PostOut } from "../api/feed";
import type { EventOut, EventRsvpOut, RsvpStatus } from "../api/events";
import type { ChirpOut } from "../api/chirps";
import type { ContentReportOut, UserBlockOut } from "../api/moderation";
import type { LineageTreeOut } from "../api/lineage";
import type { DuesCycleOut, LedgerEntryOut, SpendApprovalOut } from "../api/finance";
import type { MeetingAttendanceOut, MeetingOut } from "../api/meetings";
import type { AlumniProfileOut, JobPostOut } from "../api/alumni";

// ---------- helpers ----------

let idCounter = 0;

/** Generate a unique mock id (client-side creates while USE_MOCKS is true). */
export function newMockId(prefix: string): string {
  idCounter += 1;
  return `${prefix}-mock-${idCounter}`;
}

export function nowIso(): string {
  return new Date().toISOString();
}

/**
 * Placeholder ciphertext blob (base64 of "MOCK_CIPHERTEXT_NOT_REAL_E2EE").
 * Real ciphertext comes from libsignal in milestone 3 — never from here.
 */
export const MOCK_CIPHERTEXT_B64 = "TU9DS19DSVBIRVJURVhUX05PVF9SRUFMX0UyRUU=";

// ---------- campus / chapter ----------

export const MOCK_CAMPUS: CampusOut = {
  id: "campus-uncg",
  name: "UNC Greensboro",
  slug: "uncg",
};

/** A chapter/org plus its brand colors (DESIGN §8.6). ChapterOut itself (api/chapters.ts)
 * has no `colors` field — this mock-only extension keeps that contract untouched
 * while still being assignable anywhere a ChapterOut is expected. */
export interface MockChapter extends ChapterOut {
  colors: CampusColors;
}

export const MOCK_CHAPTER: MockChapter = {
  id: "chapter-sigchi-em",
  campus_id: MOCK_CAMPUS.id,
  org_name: "Sigma Chi",
  chapter_name: "Epsilon Mu",
  stripe_account_id: null,
  created_at: "2024-08-20T15:00:00Z",
  colors: { primary: "#1F4396", secondary: "#D6A756" }, // blue + old gold
};

/**
 * A second, unrelated org (DESIGN §8.6: "add at least one sorority... so both are
 * proven"). Jake isn't a member — this exists only so OrgAccentScope's colors can
 * be shown against a chapter that ISN'T the signed-in user's, proving the system
 * is org-agnostic rather than secretly Sigma-Chi-shaped.
 */
export const MOCK_CHAPTER_ADPI: MockChapter = {
  id: "chapter-adpi-zr",
  campus_id: MOCK_CAMPUS.id,
  org_name: "Alpha Delta Pi",
  chapter_name: "Zeta Rho",
  stripe_account_id: null,
  created_at: "2023-01-15T15:00:00Z",
  colors: { primary: "#2E9BD6", secondary: "#0B2340" }, // azure + navy
};

/**
 * Org brand colors keyed by org_name (DESIGN §8.6), used by chapter/_layout.tsx
 * to scope OrgAccentScope for the CURRENT (real) chapter — not just the two
 * seeded mocks above. A real org created outside those two (e.g. the live
 * "Chirp HQ — Founding Chapter" fixture) has no colors row in the backend yet,
 * so it falls back to the Chirp brand gradient — a real, deliberate color pair
 * rather than a null/gray theme, until orgs can set their own colors server-side.
 */
const ORG_COLORS_BY_NAME: Record<string, CampusColors> = {
  [MOCK_CHAPTER.org_name]: MOCK_CHAPTER.colors,
  [MOCK_CHAPTER_ADPI.org_name]: MOCK_CHAPTER_ADPI.colors,
};

export const DEFAULT_ORG_COLORS: CampusColors = { primary: brand, secondary: "#8B5CF6" };

export function orgColorsByName(orgName: string): CampusColors {
  return ORG_COLORS_BY_NAME[orgName] ?? DEFAULT_ORG_COLORS;
}

/** chapter_id -> campus_id, for the campus feed endpoints (GET /campuses/{campus_id}/feed)
 * to scope mock posts by campus the same way the real backend joins chapter -> campus. */
const CAMPUS_ID_BY_CHAPTER: Record<string, string> = {
  [MOCK_CHAPTER.id]: MOCK_CHAPTER.campus_id,
  [MOCK_CHAPTER_ADPI.id]: MOCK_CHAPTER_ADPI.campus_id,
};

export function mockCampusIdForChapter(chapterId: string): string | undefined {
  return CAMPUS_ID_BY_CHAPTER[chapterId];
}

// ---------- campus theming (DESIGN §8.5) ----------

/** UNCG's brand colors — Spartan navy + gold (approx. brand values; swap for the
 * official hex once we get the brand guide). Default accent/tint source before a
 * user overrides it in Appearance settings. */
export const MOCK_CAMPUS_COLORS: CampusColors = {
  primary: "#0B2340", // Spartan navy
  secondary: "#FFB71B", // Spartan gold
};

/**
 * Seed prefs for AppearanceProvider (app/_layout.tsx) — mock persistence (local
 * state) for now. Sourced from DEFAULT_APPEARANCE_PREFS (theme/appearance.tsx)
 * rather than repeated literally, so this can't drift from the §8.5 default
 * (campus primary accent + campus tint background — the app feels like the
 * school out of the box).
 */
export const mockAppearancePrefs: AppearancePrefs = DEFAULT_APPEARANCE_PREFS;

// ---------- users (current user = president) ----------

/**
 * `avatar_url` doubles as the mock "photo_url" (DESIGN §10.2): every real user
 * gets a stable `https://i.pravatar.cc/150?u=<id>` face, seeded off their own id
 * so it's stable across reloads. Ghost users are the one exception — is_ghost
 * means they never signed up, so there's no photo to have; Avatar.tsx already
 * renders ghosts as a dashed placeholder regardless of this field.
 */
function user(
  id: string,
  displayName: string,
  email: string,
  accountType: UserOut["account_type"] = "greek",
  isGhost = false,
): UserOut {
  return {
    id,
    firebase_uid: `fb-${id}`,
    email,
    display_name: displayName,
    avatar_url: isGhost ? null : `https://i.pravatar.cc/150?u=${id}`,
    account_type: accountType,
    campus_id: MOCK_CAMPUS.id,
    is_ghost: isGhost,
    suspended_at: null,
    created_at: "2024-09-01T12:00:00Z",
  };
}

export const MOCK_USERS: UserOut[] = [
  user("usr-jake", "Jake Miller", "jake.miller@uncg.edu"),
  user("usr-tyler", "Tyler Brooks", "tyler.brooks@uncg.edu"),
  user("usr-maria", "Maria Gonzalez", "maria.gonzalez@uncg.edu"),
  user("usr-devon", "Devon Carter", "devon.carter@uncg.edu"),
  user("usr-priya", "Priya Shah", "priya.shah@uncg.edu"),
  user("usr-chris", "Chris Nakamura", "chris.nakamura@uncg.edu"),
  user("usr-sam", "Sam Osei", "sam.osei@uncg.edu"),
  user("usr-ethan", "Ethan Walsh", "ethan.walsh@uncg.edu"),
  user("usr-noah", "Noah Kim", "noah.kim@uncg.edu"),
  user("usr-alexis", "Alexis Turner", "alexis.turner@alumni.uncg.edu", "alumni"),
  user("usr-jordan", "Jordan Reyes", "jordan.reyes@alumni.uncg.edu", "alumni"),
  user("usr-alumni-priya", "Priya Desai", "priya.desai@alumni.uncg.edu", "alumni"),
  user("usr-alumni-marcus", "Marcus Cole", "marcus.cole@alumni.uncg.edu", "alumni"),
  // Ghost node: placeholder for a historical member who never signed up (SPEC users.is_ghost).
  user("usr-ghost-hammer", 'Robert "Hammer" Hayes', "ghost-hammer@placeholder.invalid", "alumni", true),
  // Alpha Delta Pi (Zeta Rho) members — a second, unrelated org (§8.6 org-agnosticism proof).
  user("usr-grace", "Grace Whitfield", "grace.whitfield@uncg.edu"),
  user("usr-sofia", "Sofia Martinez", "sofia.martinez@uncg.edu"),
  user("usr-naomi", "Naomi Patel", "naomi.patel@uncg.edu"),
];

export const MOCK_CURRENT_USER: UserOut = MOCK_USERS[0]; // Jake Miller — president

export function mockUserById(userId: string): UserOut | undefined {
  return MOCK_USERS.find((u) => u.id === userId);
}

// ---------- memberships (every role represented) ----------

function membership(
  userId: string,
  role: MembershipOut["role"],
  pledgeClass: string | null,
  chapterId: string = MOCK_CHAPTER.id,
): MembershipOut {
  return {
    id: `mem-${userId}`,
    user_id: userId,
    chapter_id: chapterId,
    role,
    status: "active",
    pledge_class: pledgeClass,
    joined_at: "2024-09-05T18:00:00Z",
  };
}

export const MOCK_MEMBERSHIPS: MembershipOut[] = [
  membership("usr-jake", "president", "Fall 2023"),
  membership("usr-tyler", "vice_president", "Fall 2023"),
  membership("usr-maria", "treasurer", "Fall 2022"),
  membership("usr-devon", "secretary", "Fall 2024"),
  membership("usr-priya", "historian", "Fall 2024"),
  membership("usr-chris", "member", "Fall 2024"),
  membership("usr-sam", "member", "Fall 2024"),
  membership("usr-ethan", "pledge", "Fall 2026"),
  membership("usr-noah", "pledge", "Fall 2026"),
  membership("usr-alexis", "alumni", "Fall 2018"),
  membership("usr-jordan", "alumni", "Fall 2015"),
];

export const MOCK_CURRENT_MEMBERSHIP: MembershipOut = MOCK_MEMBERSHIPS[0];

/** Alpha Delta Pi (Zeta Rho) roster — kept as its own array (not merged into
 * MOCK_MEMBERSHIPS) since the mocked listMembers() (api/chapters.ts) returns
 * MOCK_MEMBERSHIPS unconditionally; a separate export proves
 * org-agnosticism in data without changing Jake's Sigma Chi member experience. */
export const MOCK_MEMBERSHIPS_ADPI: MembershipOut[] = [
  membership("usr-grace", "president", "Fall 2022", MOCK_CHAPTER_ADPI.id),
  membership("usr-sofia", "member", "Fall 2024", MOCK_CHAPTER_ADPI.id),
  membership("usr-naomi", "member", "Fall 2025", MOCK_CHAPTER_ADPI.id),
];

/** Mocked GET /chapters/{id}/role-meta as the PRESIDENT sees it (Jake) — matches the
 * backend rule: president may mint every role, common roles listed first. */
export const MOCK_ROLE_META: RoleMetaOut = {
  roles: ["president", "vice_president", "treasurer", "secretary", "historian", "member", "pledge", "alumni"],
  eboard: ["president", "vice_president", "treasurer", "secretary", "historian"],
  invitable: ["member", "pledge", "alumni", "president", "vice_president", "treasurer", "secretary", "historian"],
  // c80: a president holds every capability. Kept in the same order the server
  // sorts them so a diff against a real response reads cleanly.
  capabilities: ["dues_admin", "members_admin", "minutes_admin", "moderation"],
};

export const MOCK_INVITES: ChapterInviteOut[] = [
  {
    id: "inv-1",
    chapter_id: MOCK_CHAPTER.id,
    code: "SIGCHI-EM-F26",
    role: "pledge",
    expires_at: "2026-10-01T00:00:00Z",
    max_uses: 25,
    uses: 3,
    revoked_at: null,
    created_by: "usr-jake",
  },
];

// ---------- feed (DESIGN §7 FYP: text / photo / video, tagged by source for the filter pills) ----------

export const MOCK_POSTS: PostOut[] = [
  {
    id: "post-1",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-priya",
    body: "Composite photos are IN. Come by the house library to see the wall of shame (and glory). Fall 2024 might be the best-looking class yet, no bias.",
    media_urls: null,
    created_at: "2026-08-09T21:14:00Z",
    deleted_at: null,
    post_type: "text",
    audience: "org",
  },
  {
    id: "post-2",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-devon",
    body: "Chapter meeting moved to 7pm Sunday. Attendance is mandatory for actives, pledges welcome. Minutes from last week are posted in the secretary dashboard.",
    media_urls: null,
    created_at: "2026-08-10T16:30:00Z",
    deleted_at: null,
    post_type: "text",
    audience: "org",
  },
  {
    id: "post-3",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-jake",
    body: "Huge shoutout to everyone who showed up for the Peabody Park cleanup this morning. 42 bags of trash. Philanthropy chair says we beat the Delts' number by double.",
    media_urls: null,
    created_at: "2026-08-11T13:05:00Z",
    deleted_at: null,
    post_type: "text",
    audience: "org",
  },
  {
    id: "post-4",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-jordan",
    body: "First week back on campus and College Ave already looks unreal. Miss this place.",
    media_urls: ["https://picsum.photos/seed/241/700/500"],
    created_at: "2026-08-11T15:40:00Z",
    deleted_at: null,
    post_type: "photo",
    audience: "campus",
  },
  {
    id: "post-5",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-noah",
    body: "Campus radio's live set on the EUC lawn tonight, swing by after dinner.",
    media_urls: ["https://picsum.photos/seed/242/700/500"],
    created_at: "2026-08-11T18:05:00Z",
    deleted_at: null,
    post_type: "video",
    duration_sec: 47,
    audience: "campus",
  },
  {
    id: "post-6",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-alexis",
    body: "Recruiting a few sophomores/juniors for a summer analyst class. DM if interested, doesn't have to be finance majors.",
    media_urls: null,
    created_at: "2026-08-11T19:20:00Z",
    deleted_at: null,
    post_type: "text",
    audience: "campus",
  },
  {
    id: "post-7",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-tyler",
    body: "New composite frame is up in the chapter room. Looks sharp.",
    media_urls: ["https://picsum.photos/seed/243/700/500"],
    created_at: "2026-08-11T20:10:00Z",
    deleted_at: null,
    post_type: "photo",
    audience: "org",
  },
  {
    id: "post-8",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-devon",
    body: "Timelapse of the Peabody Park cleanup crew this morning, 42 bags in under two hours.",
    media_urls: ["https://picsum.photos/seed/244/700/500"],
    created_at: "2026-08-12T09:00:00Z",
    deleted_at: null,
    post_type: "video",
    duration_sec: 22,
    audience: "campus",
  },
  {
    id: "post-9",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-ethan",
    body: "Anyone know if the Kaplan Center pool is open this weekend? Website says maintenance but that's been up since June.",
    media_urls: null,
    created_at: "2026-08-12T10:15:00Z",
    deleted_at: null,
    post_type: "text",
    audience: "campus",
  },
];

export const MOCK_POST_LIKES: PostLikeOut[] = [
  { post_id: "post-3", user_id: "usr-maria", created_at: "2026-08-11T13:10:00Z" },
  { post_id: "post-3", user_id: "usr-chris", created_at: "2026-08-11T13:12:00Z" },
  { post_id: "post-3", user_id: "usr-sam", created_at: "2026-08-11T13:15:00Z" },
  { post_id: "post-3", user_id: "usr-ethan", created_at: "2026-08-11T13:21:00Z" },
  { post_id: "post-1", user_id: "usr-jake", created_at: "2026-08-09T21:30:00Z" },
  { post_id: "post-1", user_id: "usr-tyler", created_at: "2026-08-09T22:02:00Z" },
  { post_id: "post-2", user_id: "usr-jake", created_at: "2026-08-10T16:45:00Z" },
  { post_id: "post-4", user_id: "usr-jake", created_at: "2026-08-11T15:55:00Z" },
  { post_id: "post-5", user_id: "usr-priya", created_at: "2026-08-11T18:20:00Z" },
  { post_id: "post-5", user_id: "usr-chris", created_at: "2026-08-11T18:22:00Z" },
  { post_id: "post-7", user_id: "usr-maria", created_at: "2026-08-11T20:25:00Z" },
  { post_id: "post-8", user_id: "usr-priya", created_at: "2026-08-12T09:10:00Z" },
  { post_id: "post-org-1", user_id: "usr-ethan", created_at: "2026-08-11T22:10:00Z" },
  { post_id: "post-org-1", user_id: "usr-noah", created_at: "2026-08-11T22:12:00Z" },
  { post_id: "post-org-2", user_id: "usr-jake", created_at: "2026-08-12T14:35:00Z" },
  { post_id: "post-org-2", user_id: "usr-tyler", created_at: "2026-08-12T14:40:00Z" },
  { post_id: "post-org-2", user_id: "usr-chris", created_at: "2026-08-12T14:50:00Z" },
  { post_id: "post-org-4", user_id: "usr-maria", created_at: "2026-08-12T18:50:00Z" },
];

// display_name/avatar_url mirror the MOCK_USERS rows these author_ids point at, since
// c228 made them part of the wire shape — a fixture that dropped them would describe a
// response the backend can no longer send.
export const MOCK_POST_COMMENTS: PostCommentOut[] = [
  {
    id: "cmt-1",
    post_id: "post-1",
    author_id: "usr-chris",
    display_name: "Chris Nakamura",
    avatar_url: "https://i.pravatar.cc/150?u=usr-chris",
    body: "My eyes were closed in mine. Requesting a reshoot.",
    created_at: "2026-08-09T21:40:00Z",
    deleted_at: null,
  },
  {
    id: "cmt-2",
    post_id: "post-1",
    author_id: "usr-priya",
    display_name: "Priya Shah",
    avatar_url: "https://i.pravatar.cc/150?u=usr-priya",
    body: "The composite is FINAL, Chris.",
    created_at: "2026-08-09T21:44:00Z",
    deleted_at: null,
  },
  {
    id: "cmt-3",
    post_id: "post-3",
    author_id: "usr-alexis",
    display_name: "Alexis Turner",
    avatar_url: "https://i.pravatar.cc/150?u=usr-alexis",
    body: "Proud of you guys. It was 12 bags back in my day.",
    created_at: "2026-08-11T14:00:00Z",
    deleted_at: null,
  },
  {
    id: "cmt-4",
    post_id: "post-6",
    author_id: "usr-sam",
    display_name: "Sam Osei",
    avatar_url: "https://i.pravatar.cc/150?u=usr-sam",
    body: "Applying tonight, thank you!",
    created_at: "2026-08-11T19:30:00Z",
    deleted_at: null,
  },
  {
    id: "cmt-5",
    post_id: "post-9",
    author_id: "usr-noah",
    display_name: "Noah Kim",
    avatar_url: "https://i.pravatar.cc/150?u=usr-noah",
    body: "Yeah still closed, saw the sign this morning.",
    created_at: "2026-08-12T10:20:00Z",
    deleted_at: null,
  },
  {
    id: "cmt-org-1",
    post_id: "post-org-2",
    author_id: "usr-chris",
    display_name: "Chris Nakamura",
    avatar_url: "https://i.pravatar.cc/150?u=usr-chris",
    body: "not a word out of me, promise",
    created_at: "2026-08-12T14:55:00Z",
    deleted_at: null,
  },
];

// ---------- org-only feed posts (DESIGN §8.7 — chapter-only, never on the FYP) ----------
// Rendered inside OrgAccentScope by the Orgs tab's Feed segment (app/(tabs)/chapter/index.tsx).
// Kept as a separate array (not merged into MOCK_POSTS) since MOCK_POSTS already carries a few
// `audience: "org"` posts for the pre-§8.7 scaffold — the Feed segment reads both.
export const MOCK_ORG_POSTS: PostOut[] = [
  {
    id: "post-org-1",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-tyler",
    body: "Ritual practice runs right after chapter meeting tonight. New members, chapter room by 7:45, jacket and tie. Actives who haven't been through in a while, come brush up too.",
    media_urls: null,
    created_at: "2026-08-11T22:00:00Z",
    deleted_at: null,
    post_type: "text",
    audience: "org",
  },
  {
    id: "post-org-2",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-priya",
    body: "Big/Little reveal is locked in for next Thursday at the house. Actives, I will know if you leak the theme, don't test me.",
    media_urls: ["https://picsum.photos/seed/sigchi-reveal/700/500"],
    created_at: "2026-08-12T14:30:00Z",
    deleted_at: null,
    post_type: "photo",
    audience: "org",
  },
  {
    id: "post-org-3",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-devon",
    body: "House kitchen deep clean Saturday morning before Monday's inspection. The sign-up sheet's on the fridge, need at least eight of you. Pledges get priority credit.",
    media_urls: null,
    created_at: "2026-08-12T16:10:00Z",
    deleted_at: null,
    post_type: "text",
    audience: "org",
  },
  {
    id: "post-org-4",
    chapter_id: MOCK_CHAPTER.id,
    campus_id: MOCK_CHAPTER.campus_id,
    author_id: "usr-jake",
    body: "Founders Day formal photographer is booked for the Blandwood Mansion lawn. Dress code and call times drop in Chapter Chat this week, so don't be the guy asking me in the group chat.",
    media_urls: ["https://picsum.photos/seed/sigchi-founders/700/500"],
    created_at: "2026-08-12T18:45:00Z",
    deleted_at: null,
    post_type: "photo",
    audience: "org",
  },
];

// ---------- moments (DESIGN §7 MomentsRow — Snapchat-style story strip; mock taps only) ----------

export interface MockMoment {
  id: string;
  userId: string;
}

export const MOCK_MOMENTS: MockMoment[] = [
  { id: "mom-1", userId: "usr-tyler" },
  { id: "mom-2", userId: "usr-maria" },
  { id: "mom-3", userId: "usr-priya" },
  { id: "mom-4", userId: "usr-devon" },
  { id: "mom-5", userId: "usr-sam" },
  { id: "mom-6", userId: "usr-ethan" },
  { id: "mom-7", userId: "usr-noah" },
];

// ---------- chirps (anonymous — NO author fields, SPEC §8.3) ----------

export const MOCK_CHIRPS: ChirpOut[] = [
  {
    id: "chirp-1",
    campus_id: MOCK_CAMPUS.id,
    body: "the EUC put out 'street tacos' today. brother, that was a fold of sadness",
    score: 41,
    created_at: "2026-08-11T12:20:00Z",
  },
  {
    id: "chirp-2",
    campus_id: MOCK_CAMPUS.id,
    body: "whoever keeps playing saxophone on Tate Street at 8am: you're getting better and I hate that I know that",
    score: 87,
    created_at: "2026-08-11T09:02:00Z",
  },
  {
    id: "chirp-3",
    campus_id: MOCK_CAMPUS.id,
    body: "Jackson Library 4th floor AC is broken again. finals week speedrun any% sweat category",
    score: 12,
    created_at: "2026-08-10T22:47:00Z",
  },
  {
    id: "chirp-4",
    campus_id: MOCK_CAMPUS.id,
    body: "hot take: the Spartan Village geese run this campus and we just live here",
    score: -3,
    created_at: "2026-08-10T18:11:00Z",
  },
];

/** The current user's own votes (chirp_id → value), so vote UI can render state. */
export const MOCK_MY_CHIRP_VOTES: Record<string, 1 | -1> = { "chirp-2": 1 };

// ---------- messaging ----------
// MOCK ONLY: mock_plaintext exists so thread screens can render. Real messages are
// E2EE ciphertext; decrypted text lives only in on-device SQLite (SPEC §2.1, §6.5).

export interface MockMessage extends MessageOut {
  /** Mock-only convenience: which user "sent" this message. */
  sender_user_id: string;
  /** Mock-only readable body — stands in for locally-decrypted plaintext. */
  mock_plaintext: string;
}

export const MOCK_CONVERSATIONS: ConversationOut[] = [
  {
    id: "cnv-group-chapter",
    chapter_id: MOCK_CHAPTER.id,
    kind: "group",
    title: "Chapter Chat",
    protocol_version: 2,
    created_at: "2025-01-15T20:00:00Z",
    members: ["usr-jake", "usr-tyler", "usr-maria", "usr-devon", "usr-priya", "usr-chris", "usr-sam"].map(
      (userId) => ({
        conversation_id: "cnv-group-chapter",
        user_id: userId,
        joined_at: "2025-01-15T20:00:00Z",
        left_at: null,
      }),
    ),
  },
  {
    id: "cnv-dm-maria",
    chapter_id: null,
    kind: "dm",
    title: null,
    protocol_version: 2,
    created_at: "2026-03-02T17:30:00Z",
    members: [
      { conversation_id: "cnv-dm-maria", user_id: "usr-jake", joined_at: "2026-03-02T17:30:00Z", left_at: null },
      { conversation_id: "cnv-dm-maria", user_id: "usr-maria", joined_at: "2026-03-02T17:30:00Z", left_at: null },
    ],
  },
];

function mockMessage(
  id: string,
  conversationId: string,
  senderUserId: string,
  plaintext: string,
  createdAt: string,
): MockMessage {
  return {
    id,
    conversation_id: conversationId,
    sender_device_id: `dev-${senderUserId}`,
    ciphertext_b64: MOCK_CIPHERTEXT_B64,
    message_type: "signal",
    created_at: createdAt,
    sender_user_id: senderUserId,
    mock_plaintext: plaintext,
  };
}

export const MOCK_MESSAGES: MockMessage[] = [
  mockMessage("msg-1", "cnv-group-chapter", "usr-jake", "Lake cleanup crew, meet at the house at 8:30, we roll at 9.", "2026-08-11T02:10:00Z"),
  mockMessage("msg-2", "cnv-group-chapter", "usr-chris", "8:30 AM?? on a SATURDAY??", "2026-08-11T02:12:00Z"),
  mockMessage("msg-3", "cnv-group-chapter", "usr-sam", "bring gloves, last year a goose fought Devon and the goose won", "2026-08-11T02:15:00Z"),
  mockMessage("msg-4", "cnv-group-chapter", "usr-devon", "that is NOT in the minutes and never will be", "2026-08-11T02:16:00Z"),
  mockMessage("msg-5", "cnv-group-chapter", "usr-maria", "Reminder that dues are in the dashboard now. Pay there, not in chat. Money in the dashboard, talk in the chat.", "2026-08-11T15:20:00Z"),
  mockMessage("msg-6", "cnv-dm-maria", "usr-maria", "Hey, can you approve the jersey spend request before Friday? Vendor needs the deposit.", "2026-08-10T19:04:00Z"),
  mockMessage("msg-7", "cnv-dm-maria", "usr-jake", "Approved it just now. Also fixed that duplicate rush catering entry, check the correction in the ledger.", "2026-08-10T19:20:00Z"),
  mockMessage("msg-8", "cnv-dm-maria", "usr-maria", "Saw it, books balance again. You're forgiven for August.", "2026-08-10T19:26:00Z"),
];

// ---------- key directory (mock device + bundle placeholders) ----------

export const MOCK_DEVICE: DeviceOut = {
  id: "dev-usr-jake",
  user_id: "usr-jake",
  device_label: "Jake's iPhone",
  registration_id: 4821,
  identity_key_b64: "bW9jay1pZGVudGl0eS1rZXk=", // base64("mock-identity-key") — placeholder, not a key
  created_at: "2026-01-10T10:00:00Z",
  revoked_at: null,
};

export const MOCK_PREKEY_BUNDLE: PrekeyBundleOut = {
  user_id: "usr-maria",
  devices: [
    {
      device_id: "dev-usr-maria",
      registration_id: 7710,
      identity_key_b64: "bW9jay1pZGVudGl0eS1rZXk=",
      signed_prekey: {
        key_id: 1,
        public_key_b64: "bW9jay1zaWduZWQtcHJla2V5",
        signature_b64: "bW9jay1zaWduYXR1cmU=",
      },
      one_time_prekey: { key_id: 42, public_key_b64: "bW9jay1vbmUtdGltZS1wcmVrZXk=" },
    },
  ],
};

// ---------- lineage (families + edges, incl. a ghost node) ----------

/**
 * Branching family trees. Each little has exactly one big (SPEC unique constraint);
 * a big may have multiple littles — that's what creates the tree shape.
 */
export const MOCK_LINEAGE_TREE: LineageTreeOut = {
  families: [
    { id: "fam-hammer", chapter_id: MOCK_CHAPTER.id, name: "Hammer Family", color: "#6366F1" },
    { id: "fam-anchor", chapter_id: MOCK_CHAPTER.id, name: "Anchor Family", color: "#F59E0B" },
  ],
  nodes: [
    { user_id: "usr-ghost-hammer", display_name: 'Robert "Hammer" Hayes', avatar_url: null, is_ghost: true, family_id: "fam-hammer", pledge_class: "Fall 1998", depth: 0 },
    { user_id: "usr-alexis", display_name: "Alexis Turner", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2018", depth: 1 },
    { user_id: "usr-jake", display_name: "Jake Miller", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2023", depth: 2 },
    { user_id: "usr-tyler", display_name: "Tyler Brooks", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2023", depth: 2 },
    { user_id: "usr-chris", display_name: "Chris Nakamura", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2024", depth: 3 },
    { user_id: "usr-ethan", display_name: "Ethan Walsh", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2026", depth: 3 },
    { user_id: "usr-jordan", display_name: "Jordan Reyes", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2015", depth: 0 },
    { user_id: "usr-maria", display_name: "Maria Gonzalez", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2022", depth: 1 },
    { user_id: "usr-sam", display_name: "Sam Osei", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2024", depth: 2 },
    { user_id: "usr-noah", display_name: "Noah Kim", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2026", depth: 2 },
    // Unplaced — omitted from the graph canvas until they get a big.
    { user_id: "usr-devon", display_name: "Devon Carter", avatar_url: null, is_ghost: false, family_id: null, pledge_class: "Fall 2024", depth: 0 },
    { user_id: "usr-priya", display_name: "Priya Shah", avatar_url: null, is_ghost: false, family_id: null, pledge_class: "Fall 2024", depth: 0 },
  ],
  edges: [
    { id: "edge-1", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-ghost-hammer", little_user_id: "usr-alexis", family_id: "fam-hammer", pledge_class: "Fall 2018", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:00:00Z" },
    // Alexis has two littles → the Hammer tree branches.
    { id: "edge-2", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-alexis", little_user_id: "usr-jake", family_id: "fam-hammer", pledge_class: "Fall 2023", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:05:00Z" },
    { id: "edge-3", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-alexis", little_user_id: "usr-tyler", family_id: "fam-hammer", pledge_class: "Fall 2023", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:06:00Z" },
    { id: "edge-4", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-tyler", little_user_id: "usr-chris", family_id: "fam-hammer", pledge_class: "Fall 2024", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-09-20T12:00:00Z" },
    // Pending confirmation: Jake claimed Ethan as his little; Ethan hasn't confirmed yet.
    { id: "edge-5", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-jake", little_user_id: "usr-ethan", family_id: "fam-hammer", pledge_class: "Fall 2026", confirmed_by_little: false, created_by: "usr-jake", created_at: "2026-08-08T12:00:00Z" },
    { id: "edge-6", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-jordan", little_user_id: "usr-maria", family_id: "fam-anchor", pledge_class: "Fall 2022", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:10:00Z" },
    // Maria has two littles → the Anchor tree branches.
    { id: "edge-7", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-maria", little_user_id: "usr-sam", family_id: "fam-anchor", pledge_class: "Fall 2024", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-09-20T12:10:00Z" },
    { id: "edge-8", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-maria", little_user_id: "usr-noah", family_id: "fam-anchor", pledge_class: "Fall 2026", confirmed_by_little: true, created_by: "usr-priya", created_at: "2026-08-08T12:10:00Z" },
  ],
};

// ---------- finance (append-only ledger incl. correction pair) ----------

export const MOCK_DUES_CYCLES: DuesCycleOut[] = [
  {
    id: "cycle-fall-2026",
    chapter_id: MOCK_CHAPTER.id,
    name: "Fall 2026 Dues",
    amount_cents: 45000,
    due_date: "2026-09-15",
    created_at: "2026-08-01T12:00:00Z",
    // The caller's own standing, server-decided since c258. Unpaid here so the mock
    // shows a cycle in the state this screen actually exists for: one still to settle.
    viewer_paid: false,
    viewer_on_plan: false,
  },
];

export const MOCK_LEDGER_ENTRIES: LedgerEntryOut[] = [
  {
    id: "led-1",
    chapter_id: MOCK_CHAPTER.id,
    entry_type: "dues_payment",
    amount_cents: 45000,
    category: "dues",
    description: "Fall 2026 dues, Chris Nakamura",
    related_user_id: "usr-chris",
    dues_cycle_id: "cycle-fall-2026",
    stripe_payment_intent_id: "pi_mock_0001",
    corrects_entry_id: null,
    created_by: "usr-maria",
    created_at: "2026-08-05T14:02:00Z",
  },
  {
    id: "led-2",
    chapter_id: MOCK_CHAPTER.id,
    entry_type: "dues_payment",
    amount_cents: 45000,
    category: "dues",
    description: "Fall 2026 dues, Sam Osei",
    related_user_id: "usr-sam",
    dues_cycle_id: "cycle-fall-2026",
    stripe_payment_intent_id: "pi_mock_0002",
    corrects_entry_id: null,
    created_by: "usr-maria",
    created_at: "2026-08-06T10:31:00Z",
  },
  // Correction pair: led-3 was entered by mistake (duplicate), led-4 reverses it.
  // Append-only ledger: never UPDATE/DELETE — corrections reference corrects_entry_id (SPEC §8.2).
  {
    id: "led-3",
    chapter_id: MOCK_CHAPTER.id,
    entry_type: "expense",
    amount_cents: -25000,
    category: "rush",
    description: "Rush week catering (duplicate entry, see led-4)",
    related_user_id: null,
    dues_cycle_id: null,
    stripe_payment_intent_id: null,
    corrects_entry_id: null,
    created_by: "usr-maria",
    created_at: "2026-08-07T16:00:00Z",
  },
  {
    id: "led-4",
    chapter_id: MOCK_CHAPTER.id,
    entry_type: "correction",
    amount_cents: 25000,
    category: "rush",
    description: "Reverses duplicate rush catering entry",
    related_user_id: null,
    dues_cycle_id: null,
    stripe_payment_intent_id: null,
    corrects_entry_id: "led-3",
    created_by: "usr-maria",
    created_at: "2026-08-10T19:15:00Z",
  },
  {
    id: "led-5",
    chapter_id: MOCK_CHAPTER.id,
    entry_type: "expense",
    amount_cents: -12500,
    category: "rush",
    description: "Rush week catering, Tony's Deli",
    related_user_id: null,
    dues_cycle_id: null,
    stripe_payment_intent_id: null,
    corrects_entry_id: null,
    created_by: "usr-maria",
    created_at: "2026-08-07T16:05:00Z",
  },
  {
    id: "led-6",
    chapter_id: MOCK_CHAPTER.id,
    entry_type: "budget_allocation",
    amount_cents: -30000,
    category: "social",
    description: "Fall social budget allocation",
    related_user_id: null,
    dues_cycle_id: null,
    stripe_payment_intent_id: null,
    corrects_entry_id: null,
    created_by: "usr-maria",
    created_at: "2026-08-02T09:00:00Z",
  },
];

export const MOCK_SPEND_APPROVALS: SpendApprovalOut[] = [
  {
    id: "spend-1",
    chapter_id: MOCK_CHAPTER.id,
    requested_by: "usr-chris",
    amount_cents: 20000,
    description: "Intramural jerseys deposit (vendor: Court Kings)",
    status: "approved",
    decided_by: "usr-jake",
    decided_at: "2026-08-10T19:18:00Z",
    created_at: "2026-08-09T11:00:00Z",
  },
  {
    id: "spend-2",
    chapter_id: MOCK_CHAPTER.id,
    requested_by: "usr-sam",
    amount_cents: 15000,
    description: "Decorations + speakers rental for the back-to-school mixer",
    status: "pending",
    decided_by: null,
    decided_at: null,
    created_at: "2026-08-11T10:45:00Z",
  },
];

// ---------- meetings + attendance ----------

export const MOCK_MEETINGS: MeetingOut[] = [
  {
    id: "mtg-1",
    chapter_id: MOCK_CHAPTER.id,
    title: "Weekly Chapter Meeting",
    meeting_date: "2026-08-03T23:00:00Z",
    minutes_md:
      "## Weekly Chapter Meeting, Aug 3\n\n- **Dues:** Fall 2026 cycle opens in the app this week (Maria).\n- **Rush:** catering booked; budget approved.\n- **Philanthropy:** lake cleanup Saturday, meet 8:30 AM.\n- **Old business:** composite photos arriving; Priya coordinating.\n",
    created_by: "usr-devon",
    created_at: "2026-08-04T01:30:00Z",
  },
  {
    id: "mtg-2",
    chapter_id: MOCK_CHAPTER.id,
    title: "E-Board Sync",
    meeting_date: "2026-08-10T22:00:00Z",
    minutes_md:
      "## E-Board Sync, Aug 10\n\n- Reviewed pending spend approvals (jerseys approved).\n- Ledger correction posted for duplicate rush entry.\n- Pledge class onboarding checklist assigned to Tyler.\n",
    created_by: "usr-devon",
    created_at: "2026-08-10T23:45:00Z",
  },
];

export const MOCK_ATTENDANCE: MeetingAttendanceOut[] = [
  { meeting_id: "mtg-1", user_id: "usr-jake", status: "present" },
  { meeting_id: "mtg-1", user_id: "usr-tyler", status: "present" },
  { meeting_id: "mtg-1", user_id: "usr-maria", status: "present" },
  { meeting_id: "mtg-1", user_id: "usr-devon", status: "present" },
  { meeting_id: "mtg-1", user_id: "usr-priya", status: "excused" },
  { meeting_id: "mtg-1", user_id: "usr-chris", status: "present" },
  { meeting_id: "mtg-1", user_id: "usr-sam", status: "absent" },
  { meeting_id: "mtg-1", user_id: "usr-ethan", status: "present" },
  { meeting_id: "mtg-1", user_id: "usr-noah", status: "present" },
  { meeting_id: "mtg-2", user_id: "usr-jake", status: "present" },
  { meeting_id: "mtg-2", user_id: "usr-tyler", status: "present" },
  { meeting_id: "mtg-2", user_id: "usr-maria", status: "present" },
  { meeting_id: "mtg-2", user_id: "usr-devon", status: "present" },
  { meeting_id: "mtg-2", user_id: "usr-priya", status: "present" },
];

// ---------- alumni + jobs ----------

export const MOCK_ALUMNI_PROFILES: AlumniProfileOut[] = [
  {
    user_id: "usr-alexis",
    grad_year: 2022,
    company: "Northgate Capital",
    title: "Analyst",
    industry: "Finance",
    location: "Chicago, IL",
    linkedin_url: "https://www.linkedin.com/in/alexis-turner-mock",
    open_to_mentoring: true,
    display_name: "Alexis Turner",
    email: "alexis.turner@alumni.uncg.edu",
  },
  {
    user_id: "usr-jordan",
    grad_year: 2019,
    company: "Harbor Health",
    title: "Product Manager",
    industry: "Healthcare Tech",
    location: "Austin, TX",
    linkedin_url: "https://www.linkedin.com/in/jordan-reyes-mock",
    open_to_mentoring: true,
    display_name: "Jordan Reyes",
    email: "jordan.reyes@alumni.uncg.edu",
  },
  {
    user_id: "usr-alumni-priya",
    grad_year: 2017,
    company: "Stripe",
    title: "Software Engineer",
    industry: "Fintech",
    location: "San Francisco, CA",
    linkedin_url: "https://www.linkedin.com/in/priya-shah-alumni-mock",
    open_to_mentoring: true,
    display_name: "Priya Desai",
    email: "priya.desai@alumni.uncg.edu",
  },
  {
    user_id: "usr-alumni-marcus",
    grad_year: 2014,
    company: "Deloitte",
    title: "Senior Consultant",
    industry: "Consulting",
    location: "New York, NY",
    linkedin_url: "https://www.linkedin.com/in/marcus-cole-mock",
    open_to_mentoring: false,
    display_name: "Marcus Cole",
    email: "marcus.cole@alumni.uncg.edu",
  },
];

export const MOCK_JOB_POSTS: JobPostOut[] = [
  {
    id: "job-1",
    posted_by: "usr-alexis",
    posted_by_name: "Alexis Nguyen",
    chapter_id: MOCK_CHAPTER.id,
    title: "Summer Analyst Intern",
    company: "Northgate Capital",
    location: "Chicago, IL",
    description:
      "Paid summer internship on the private credit team. Sophomores/juniors preferred; finance major not required. Brothers get a guaranteed first-round interview.",
    apply_url: "https://careers.northgate.example/summer-analyst",
    created_at: "2026-08-05T15:00:00Z",
    expires_at: "2026-09-30T00:00:00Z",
  },
  {
    id: "job-2",
    posted_by: "usr-jordan",
    posted_by_name: "Jordan Vance",
    chapter_id: null, // network-wide
    title: "Associate Product Manager (New Grad)",
    company: "Harbor Health",
    location: "Austin, TX (hybrid)",
    description:
      "Rotational APM program for 2027 grads. Ping me on here if you apply and I'll flag your resume.",
    apply_url: "https://harborhealth.example/careers/apm",
    created_at: "2026-08-08T18:00:00Z",
    expires_at: null,
  },
  {
    id: "job-3",
    posted_by: "usr-alumni-priya",
    posted_by_name: "Priya Raman",
    chapter_id: MOCK_CHAPTER.id,
    title: "Backend Engineer (New Grad)",
    company: "Stripe",
    location: "San Francisco, CA / Remote US",
    description:
      "Building payments infrastructure. Looking for strong systems instincts. CS major preferred but not required. Happy to refer chapter brothers.",
    apply_url: "https://stripe.com/jobs/listing/backend-engineer-new-grad",
    created_at: "2026-08-10T12:00:00Z",
    expires_at: "2026-10-15T00:00:00Z",
  },
];

// ---------- moderation ----------

export const MOCK_REPORTS: ContentReportOut[] = [
  {
    id: "rpt-1",
    reporter_id: "usr-sam",
    target_type: "chirp",
    target_id: "chirp-4",
    forwarded_plaintext: null,
    reason: "Goose slander",
    status: "dismissed",
    created_at: "2026-08-10T19:00:00Z",
  },
];

export const MOCK_BLOCKS: UserBlockOut[] = [];

// ---------- profile layout (UI-only: user-arrangeable Profile sections, DESIGN §7) ----------

export type ProfileSectionKey = "about" | "orgs" | "activity" | "alumni" | "settings";

export interface ProfileSectionLayout {
  key: ProfileSectionKey;
  visible: boolean;
}

/**
 * Seed order + visibility for the Profile screen's "Edit layout" mode. The screen
 * copies this into local state and reorders/toggles it there — mock persistence
 * for now, real per-user prefs later.
 */
export const mockProfileLayout: ProfileSectionLayout[] = [
  { key: "about", visible: true },
  { key: "orgs", visible: true },
  { key: "activity", visible: true },
  { key: "alumni", visible: true },
  { key: "settings", visible: true },
];

// ---------- org events (DESIGN §8.7 — "the Partiful corner") ----------

export const MOCK_ORG_EVENTS: EventOut[] = [
  {
    id: "evt-rush-cookout",
    chapter_id: MOCK_CHAPTER.id,
    title: "Rush Week Cookout at Peabody Park",
    cover_url: "https://picsum.photos/seed/sigchi-cookout/900/600",
    description: "Burgers, cornhole and the annual tug-of-war. Bring a friend.",
    starts_at: "2026-08-15T16:00:00Z",
    ends_at: "2026-08-15T20:00:00Z",
    visibility: "campus",
    canceled_at: null,
    location: "Peabody Park, UNCG",
    host_id: "usr-tyler",
    created_at: "2026-08-05T12:00:00Z",
  },
  {
    id: "evt-founders-formal",
    chapter_id: MOCK_CHAPTER.id,
    title: "Founders Day Formal",
    cover_url: "https://picsum.photos/seed/sigchi-formal/900/600",
    description: null,
    starts_at: "2026-09-27T23:00:00Z",
    ends_at: null,
    visibility: "chapter",
    canceled_at: null,
    location: "Blandwood Mansion, Greensboro",
    host_id: "usr-jake",
    created_at: "2026-08-06T12:00:00Z",
  },
  {
    id: "evt-homecoming-tailgate",
    chapter_id: MOCK_CHAPTER.id,
    title: "Homecoming Tailgate",
    cover_url: "https://picsum.photos/seed/sigchi-tailgate/900/600",
    description: "Tailgate opens two hours before kickoff.",
    starts_at: "2026-10-24T14:00:00Z",
    ends_at: null,
    visibility: "public",
    canceled_at: null,
    location: "Sigma Chi House, College Ave",
    host_id: "usr-priya",
    created_at: "2026-08-07T12:00:00Z",
  },
];

function eventRsvp(
  eventId: string,
  userId: string,
  status: RsvpStatus,
  createdAt: string,
): EventRsvpOut {
  return { event_id: eventId, user_id: userId, status, created_at: createdAt };
}

/**
 * Jake (current user) is deliberately left un-RSVP'd for Founders Day Formal so
 * the event detail screen demos both the "picked" and "pick one" RSVP states.
 */
export const MOCK_ORG_EVENT_RSVPS: EventRsvpOut[] = [
  // Rush Week Cookout at Peabody Park
  eventRsvp("evt-rush-cookout", "usr-jake", "going", "2026-08-05T13:00:00Z"),
  eventRsvp("evt-rush-cookout", "usr-tyler", "going", "2026-08-05T13:05:00Z"),
  eventRsvp("evt-rush-cookout", "usr-maria", "going", "2026-08-05T14:00:00Z"),
  eventRsvp("evt-rush-cookout", "usr-devon", "going", "2026-08-05T15:00:00Z"),
  eventRsvp("evt-rush-cookout", "usr-priya", "going", "2026-08-06T09:00:00Z"),
  eventRsvp("evt-rush-cookout", "usr-chris", "going", "2026-08-06T10:00:00Z"),
  eventRsvp("evt-rush-cookout", "usr-sam", "maybe", "2026-08-06T11:00:00Z"),
  eventRsvp("evt-rush-cookout", "usr-ethan", "maybe", "2026-08-06T12:00:00Z"),
  eventRsvp("evt-rush-cookout", "usr-noah", "cant", "2026-08-06T13:00:00Z"),
  // Founders Day Formal
  eventRsvp("evt-founders-formal", "usr-tyler", "going", "2026-08-06T13:00:00Z"),
  eventRsvp("evt-founders-formal", "usr-maria", "going", "2026-08-06T14:00:00Z"),
  eventRsvp("evt-founders-formal", "usr-priya", "going", "2026-08-06T15:00:00Z"),
  eventRsvp("evt-founders-formal", "usr-alexis", "going", "2026-08-07T09:00:00Z"),
  eventRsvp("evt-founders-formal", "usr-jordan", "going", "2026-08-07T10:00:00Z"),
  eventRsvp("evt-founders-formal", "usr-devon", "maybe", "2026-08-07T11:00:00Z"),
  eventRsvp("evt-founders-formal", "usr-chris", "maybe", "2026-08-07T12:00:00Z"),
  eventRsvp("evt-founders-formal", "usr-ethan", "cant", "2026-08-07T13:00:00Z"),
  // Homecoming Tailgate
  eventRsvp("evt-homecoming-tailgate", "usr-jake", "going", "2026-08-07T13:00:00Z"),
  eventRsvp("evt-homecoming-tailgate", "usr-tyler", "going", "2026-08-07T14:00:00Z"),
  eventRsvp("evt-homecoming-tailgate", "usr-devon", "going", "2026-08-07T15:00:00Z"),
  eventRsvp("evt-homecoming-tailgate", "usr-chris", "going", "2026-08-08T09:00:00Z"),
  eventRsvp("evt-homecoming-tailgate", "usr-sam", "going", "2026-08-08T10:00:00Z"),
  eventRsvp("evt-homecoming-tailgate", "usr-ethan", "going", "2026-08-08T11:00:00Z"),
  eventRsvp("evt-homecoming-tailgate", "usr-noah", "going", "2026-08-08T12:00:00Z"),
  eventRsvp("evt-homecoming-tailgate", "usr-priya", "maybe", "2026-08-08T13:00:00Z"),
];

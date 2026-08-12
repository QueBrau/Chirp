/** One coherent mock world for the Chirp app — served by src/api/* while USE_MOCKS is true.
 *
 * Lakeview State University → Sigma Chi, Epsilon Mu chapter. Current user is the
 * president (Jake Miller). Everything here is MOCK DATA: message "plaintext" is
 * included only so screens can render — real messages are E2EE and their
 * plaintext only ever exists in the on-device SQLite store (SPEC §2.1, §6).
 *
 * Type-only imports from ../api/* keep this module cycle-free at runtime.
 */

import type { CampusOut, UserOut } from "../api/auth";
import type { ChapterInviteOut, ChapterOut, MembershipOut } from "../api/chapters";
import type { DeviceOut, PrekeyBundleOut } from "../api/keys";
import type { ConversationOut, MessageOut } from "../api/messages";
import type { PostCommentOut, PostLikeOut, PostOut } from "../api/feed";
import type { YakOut } from "../api/yaks";
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
  id: "campus-lakeview",
  name: "Lakeview State University",
  slug: "lakeview-state",
};

export const MOCK_CHAPTER: ChapterOut = {
  id: "chapter-sigchi-em",
  campus_id: MOCK_CAMPUS.id,
  org_name: "Sigma Chi",
  chapter_name: "Epsilon Mu",
  stripe_account_id: null,
  created_at: "2024-08-20T15:00:00Z",
};

// ---------- users (current user = president) ----------

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
    avatar_url: null,
    account_type: accountType,
    campus_id: MOCK_CAMPUS.id,
    is_ghost: isGhost,
    created_at: "2024-09-01T12:00:00Z",
  };
}

export const MOCK_USERS: UserOut[] = [
  user("usr-jake", "Jake Miller", "jake.miller@lakeview.edu"),
  user("usr-tyler", "Tyler Brooks", "tyler.brooks@lakeview.edu"),
  user("usr-maria", "Maria Gonzalez", "maria.gonzalez@lakeview.edu"),
  user("usr-devon", "Devon Carter", "devon.carter@lakeview.edu"),
  user("usr-priya", "Priya Shah", "priya.shah@lakeview.edu"),
  user("usr-chris", "Chris Nakamura", "chris.nakamura@lakeview.edu"),
  user("usr-sam", "Sam Osei", "sam.osei@lakeview.edu"),
  user("usr-ethan", "Ethan Walsh", "ethan.walsh@lakeview.edu"),
  user("usr-noah", "Noah Kim", "noah.kim@lakeview.edu"),
  user("usr-alexis", "Alexis Turner", "alexis.turner@alumni.lakeview.edu", "alumni"),
  user("usr-jordan", "Jordan Reyes", "jordan.reyes@alumni.lakeview.edu", "alumni"),
  // Ghost node: placeholder for a historical member who never signed up (SPEC users.is_ghost).
  user("usr-ghost-hammer", 'Robert "Hammer" Hayes', "ghost-hammer@placeholder.invalid", "alumni", true),
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
): MembershipOut {
  return {
    id: `mem-${userId}`,
    user_id: userId,
    chapter_id: MOCK_CHAPTER.id,
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

/**
 * UI-only mock flag for the Orgs tab: true renders the member org hub, false
 * renders the "Find your org" discovery state (DESIGN §6). Typed as plain
 * boolean (not the literal) so both branches stay type-live in screens.
 */
export const mockIsOrgMember: boolean = true;

export const MOCK_INVITES: ChapterInviteOut[] = [
  {
    id: "inv-1",
    chapter_id: MOCK_CHAPTER.id,
    code: "SIGCHI-EM-F26",
    role: "pledge",
    expires_at: "2026-10-01T00:00:00Z",
    created_by: "usr-jake",
  },
];

// ---------- feed ----------

export const MOCK_POSTS: PostOut[] = [
  {
    id: "post-1",
    chapter_id: MOCK_CHAPTER.id,
    author_id: "usr-priya",
    body: "Composite photos are IN. Come by the house library to see the wall of shame (and glory). Fall 2024 might be the best-looking class yet, no bias.",
    media_urls: null,
    created_at: "2026-08-09T21:14:00Z",
    deleted_at: null,
  },
  {
    id: "post-2",
    chapter_id: MOCK_CHAPTER.id,
    author_id: "usr-devon",
    body: "Chapter meeting moved to 7pm Sunday — attendance is mandatory for actives, pledges welcome. Minutes from last week are posted in the secretary dashboard.",
    media_urls: null,
    created_at: "2026-08-10T16:30:00Z",
    deleted_at: null,
  },
  {
    id: "post-3",
    chapter_id: MOCK_CHAPTER.id,
    author_id: "usr-jake",
    body: "Huge shoutout to everyone who showed up for the lake cleanup this morning. 42 bags of trash. Philanthropy chair says we beat the Delts' number by double.",
    media_urls: null,
    created_at: "2026-08-11T13:05:00Z",
    deleted_at: null,
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
];

export const MOCK_POST_COMMENTS: PostCommentOut[] = [
  {
    id: "cmt-1",
    post_id: "post-1",
    author_id: "usr-chris",
    body: "My eyes were closed in mine. Requesting a reshoot.",
    created_at: "2026-08-09T21:40:00Z",
    deleted_at: null,
  },
  {
    id: "cmt-2",
    post_id: "post-1",
    author_id: "usr-priya",
    body: "The composite is FINAL, Chris.",
    created_at: "2026-08-09T21:44:00Z",
    deleted_at: null,
  },
  {
    id: "cmt-3",
    post_id: "post-3",
    author_id: "usr-alexis",
    body: "Proud of you guys. It was 12 bags back in my day.",
    created_at: "2026-08-11T14:00:00Z",
    deleted_at: null,
  },
];

// ---------- yaks (anonymous — NO author fields, SPEC §8.3) ----------

export const MOCK_YAKS: YakOut[] = [
  {
    id: "yak-1",
    campus_id: MOCK_CAMPUS.id,
    body: "the dining hall put out 'street tacos' today. brother, that was a fold of sadness",
    score: 41,
    created_at: "2026-08-11T12:20:00Z",
  },
  {
    id: "yak-2",
    campus_id: MOCK_CAMPUS.id,
    body: "whoever keeps playing saxophone in the quad at 8am: you're getting better and I hate that I know that",
    score: 87,
    created_at: "2026-08-11T09:02:00Z",
  },
  {
    id: "yak-3",
    campus_id: MOCK_CAMPUS.id,
    body: "library 4th floor AC is broken again. finals week speedrun any% sweat category",
    score: 12,
    created_at: "2026-08-10T22:47:00Z",
  },
  {
    id: "yak-4",
    campus_id: MOCK_CAMPUS.id,
    body: "hot take: the lakeview geese run this campus and we just live here",
    score: -3,
    created_at: "2026-08-10T18:11:00Z",
  },
];

/** The current user's own votes (yak_id → value), so vote UI can render state. */
export const MOCK_MY_YAK_VOTES: Record<string, 1 | -1> = { "yak-2": 1 };

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
  mockMessage("msg-1", "cnv-group-chapter", "usr-jake", "Lake cleanup crew — meet at the house at 8:30, we roll at 9.", "2026-08-11T02:10:00Z"),
  mockMessage("msg-2", "cnv-group-chapter", "usr-chris", "8:30 AM?? on a SATURDAY??", "2026-08-11T02:12:00Z"),
  mockMessage("msg-3", "cnv-group-chapter", "usr-sam", "bring gloves, last year a goose fought Devon and the goose won", "2026-08-11T02:15:00Z"),
  mockMessage("msg-4", "cnv-group-chapter", "usr-devon", "that is NOT in the minutes and never will be", "2026-08-11T02:16:00Z"),
  mockMessage("msg-5", "cnv-group-chapter", "usr-maria", "Reminder that dues are in the dashboard now — pay there, not in chat. Money in the dashboard, talk in the chat.", "2026-08-11T15:20:00Z"),
  mockMessage("msg-6", "cnv-dm-maria", "usr-maria", "Hey — can you approve the jersey spend request before Friday? Vendor needs the deposit.", "2026-08-10T19:04:00Z"),
  mockMessage("msg-7", "cnv-dm-maria", "usr-jake", "Approved it just now. Also fixed that duplicate rush catering entry — check the correction in the ledger.", "2026-08-10T19:20:00Z"),
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

export const MOCK_LINEAGE_TREE: LineageTreeOut = {
  families: [
    { id: "fam-hammer", chapter_id: MOCK_CHAPTER.id, name: "Hammer Family", color: "#6366F1" },
    { id: "fam-anchor", chapter_id: MOCK_CHAPTER.id, name: "Anchor Family", color: "#F59E0B" },
  ],
  nodes: [
    { user_id: "usr-ghost-hammer", display_name: 'Robert "Hammer" Hayes', avatar_url: null, is_ghost: true, family_id: "fam-hammer", pledge_class: "Fall 1998" },
    { user_id: "usr-alexis", display_name: "Alexis Turner", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2018" },
    { user_id: "usr-jake", display_name: "Jake Miller", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2023" },
    { user_id: "usr-tyler", display_name: "Tyler Brooks", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2023" },
    { user_id: "usr-chris", display_name: "Chris Nakamura", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2024" },
    { user_id: "usr-ethan", display_name: "Ethan Walsh", avatar_url: null, is_ghost: false, family_id: "fam-hammer", pledge_class: "Fall 2026" },
    { user_id: "usr-jordan", display_name: "Jordan Reyes", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2015" },
    { user_id: "usr-maria", display_name: "Maria Gonzalez", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2022" },
    { user_id: "usr-sam", display_name: "Sam Osei", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2024" },
    { user_id: "usr-noah", display_name: "Noah Kim", avatar_url: null, is_ghost: false, family_id: "fam-anchor", pledge_class: "Fall 2026" },
    { user_id: "usr-devon", display_name: "Devon Carter", avatar_url: null, is_ghost: false, family_id: null, pledge_class: "Fall 2024" },
    { user_id: "usr-priya", display_name: "Priya Shah", avatar_url: null, is_ghost: false, family_id: null, pledge_class: "Fall 2024" },
  ],
  edges: [
    { id: "edge-1", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-ghost-hammer", little_user_id: "usr-alexis", family_id: "fam-hammer", pledge_class: "Fall 2018", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:00:00Z" },
    { id: "edge-2", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-alexis", little_user_id: "usr-jake", family_id: "fam-hammer", pledge_class: "Fall 2023", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:05:00Z" },
    { id: "edge-3", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-alexis", little_user_id: "usr-tyler", family_id: "fam-hammer", pledge_class: "Fall 2023", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:06:00Z" },
    { id: "edge-4", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-tyler", little_user_id: "usr-chris", family_id: "fam-hammer", pledge_class: "Fall 2024", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-09-20T12:00:00Z" },
    // Pending confirmation: Jake claimed Ethan as his little; Ethan hasn't confirmed yet.
    { id: "edge-5", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-jake", little_user_id: "usr-ethan", family_id: "fam-hammer", pledge_class: "Fall 2026", confirmed_by_little: false, created_by: "usr-jake", created_at: "2026-08-08T12:00:00Z" },
    { id: "edge-6", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-jordan", little_user_id: "usr-maria", family_id: "fam-anchor", pledge_class: "Fall 2022", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-02-01T12:10:00Z" },
    { id: "edge-7", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-maria", little_user_id: "usr-sam", family_id: "fam-anchor", pledge_class: "Fall 2024", confirmed_by_little: true, created_by: "usr-priya", created_at: "2025-09-20T12:10:00Z" },
    { id: "edge-8", chapter_id: MOCK_CHAPTER.id, big_user_id: "usr-sam", little_user_id: "usr-noah", family_id: "fam-anchor", pledge_class: "Fall 2026", confirmed_by_little: true, created_by: "usr-priya", created_at: "2026-08-08T12:10:00Z" },
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
  },
];

export const MOCK_LEDGER_ENTRIES: LedgerEntryOut[] = [
  {
    id: "led-1",
    chapter_id: MOCK_CHAPTER.id,
    entry_type: "dues_payment",
    amount_cents: 45000,
    category: "dues",
    description: "Fall 2026 dues — Chris Nakamura",
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
    description: "Fall 2026 dues — Sam Osei",
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
    description: "Rush week catering (duplicate entry — see led-4)",
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
    description: "Rush week catering — Tony's Deli",
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
      "## Weekly Chapter Meeting — Aug 3\n\n- **Dues:** Fall 2026 cycle opens in the app this week (Maria).\n- **Rush:** catering booked; budget approved.\n- **Philanthropy:** lake cleanup Saturday, meet 8:30 AM.\n- **Old business:** composite photos arriving; Priya coordinating.\n",
    created_by: "usr-devon",
    created_at: "2026-08-04T01:30:00Z",
  },
  {
    id: "mtg-2",
    chapter_id: MOCK_CHAPTER.id,
    title: "E-Board Sync",
    meeting_date: "2026-08-10T22:00:00Z",
    minutes_md:
      "## E-Board Sync — Aug 10\n\n- Reviewed pending spend approvals (jerseys approved).\n- Ledger correction posted for duplicate rush entry.\n- Pledge class onboarding checklist assigned to Tyler.\n",
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
    linkedin_url: "https://www.linkedin.com/in/alexis-turner-mock",
    open_to_mentoring: true,
    display_name: "Alexis Turner",
  },
  {
    user_id: "usr-jordan",
    grad_year: 2019,
    company: "Harbor Health",
    title: "Product Manager",
    industry: "Healthcare Tech",
    linkedin_url: "https://www.linkedin.com/in/jordan-reyes-mock",
    open_to_mentoring: false,
    display_name: "Jordan Reyes",
  },
];

export const MOCK_JOB_POSTS: JobPostOut[] = [
  {
    id: "job-1",
    posted_by: "usr-alexis",
    chapter_id: MOCK_CHAPTER.id,
    title: "Summer Analyst Intern",
    company: "Northgate Capital",
    description:
      "Paid summer internship on the private credit team. Sophomores/juniors preferred; finance major not required. Brothers get a guaranteed first-round interview.",
    apply_url: "https://careers.northgate.example/summer-analyst",
    created_at: "2026-08-05T15:00:00Z",
    expires_at: "2026-09-30T00:00:00Z",
  },
  {
    id: "job-2",
    posted_by: "usr-jordan",
    chapter_id: null, // network-wide
    title: "Associate Product Manager (New Grad)",
    company: "Harbor Health",
    description:
      "Rotational APM program for 2027 grads. Ping me on here if you apply and I'll flag your resume.",
    apply_url: "https://harborhealth.example/careers/apm",
    created_at: "2026-08-08T18:00:00Z",
    expires_at: null,
  },
];

// ---------- moderation ----------

export const MOCK_REPORTS: ContentReportOut[] = [
  {
    id: "rpt-1",
    reporter_id: "usr-sam",
    target_type: "yak",
    target_id: "yak-4",
    forwarded_plaintext: null,
    reason: "Goose slander",
    status: "dismissed",
    created_at: "2026-08-10T19:00:00Z",
  },
];

export const MOCK_BLOCKS: UserBlockOut[] = [];

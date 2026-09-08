/**
 * Offline regression guard for the mobile half of board c359's collection
 * continuations: the chapter tab's Feed and Events segments used to fetch only
 * page one and never continue, even though the backend had already accepted a
 * cursor on both routes for a card or more. Source-level, like
 * verify-messages-pagination.mjs and verify-invites.mjs - no network, no auth, no
 * database - so a future edit that quietly drops the cursor plumbing fails here
 * instead of silently truncating a chapter's feed or calendar again.
 *
 *   npm run verify:c359-continuation
 */
import { readFileSync } from "node:fs";

const feed = readFileSync(new URL("../src/api/feed.ts", import.meta.url), "utf8");
const events = readFileSync(new URL("../src/api/events.ts", import.meta.url), "utf8");
const chapterScreen = readFileSync(new URL("../app/(tabs)/chapter/index.tsx", import.meta.url), "utf8");

const checks = [
  // The client half of the gap: the server already accepted these on both routes
  // (feed.py list_posts since c210, events.py list_events_with_rsvps since c201),
  // and the mobile client never sent them.
  ["listPosts takes a ListFeedOptions page argument", /export async function listPosts\(\s*chapterId: string,\s*opts: ListFeedOptions/, feed],
  ["listPosts threads before_id through to the chapter-posts request",
    (text) => {
      const at = text.indexOf("/chapters/${chapterId}/posts`,");
      return at !== -1 && text.slice(at, at + 200).includes("before_id: opts.before_id");
    }, feed],
  ["listEventsWithRsvps gains typed before/beforeId page options", /interface ChapterEventsPage \{[\s\S]*?beforeId\?: string/, events],
  ["listEventsWithRsvps threads before_id through to the request", /listEventsWithRsvps\([\s\S]{0,400}?before_id: paired \? page\.beforeId/, events],

  // The UI continuation itself - OrgFeedSegment and OrgEventsSegment must actually
  // hold a cursor/more/pending state and pass it back on the next call, not just
  // reference `hasOlder` cosmetically.
  ["OrgFeedSegment calls listPosts with a page-shaped options object", /listPosts\(chapterId,\s*\{/, chapterScreen],
  ["OrgFeedSegment has a load-older-posts continuation", /loadOlder[\s\S]{0,20}=[\s\S]*?listPosts\(chapterId,\s*\{[\s\S]*?before:/, chapterScreen],
  ["OrgEventsSegment calls listEventsWithRsvps with a page-shaped options object", /listEventsWithRsvps\(chapterId,\s*\{/, chapterScreen],
  ["OrgEventsSegment has a load-older-events continuation", /listEventsWithRsvps\(chapterId,\s*\{[\s\S]*?before: cursor\.before/, chapterScreen],
  ["a load-older-events button label exists", /Load older events/, chapterScreen],
  ["a load-older-posts button label exists", /Load older posts/, chapterScreen],

  // c358 (PR #254) added collectionPages.ts's cursor/more/pending page shape
  // precisely so a new continuation would not hand-roll its own hasOlder booleans;
  // this proves the three c359 continuations actually adopted it rather than
  // reinventing the state machine a fourth time.
  ["chapter/index.tsx uses collectionPages.ts's page primitives", /from "@\/lib\/collectionPages"/, chapterScreen],
  ["appended pages are merged through mergePageRows (no duplicate rows)", /mergePageRows\(/, chapterScreen],
];

let failures = 0;
for (const [label, check, source] of checks) {
  const pass = typeof check === "function" ? check(source) : check.test(source);
  if (pass) {
    console.log(`  PASS  ${label}`);
  } else {
    failures++;
    console.log(`  FAIL  ${label}`);
  }
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

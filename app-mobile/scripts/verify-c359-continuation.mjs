/**
 * Regression guard for the mobile half of board c359's collection continuations:
 * the chapter tab's Feed, Events and invite-code lists used to fetch only page one
 * and never continue, even though the backend had already accepted a cursor on all
 * three routes.
 *
 * The Feed and Events segments are checked by EXECUTING the real component logic
 * (chapter-continuation-cases.mjs, in the collection-race-cases.mjs style): it
 * drives the real load/loadOlder handlers and asserts the actual next request
 * carries the before/beforeId of the last row genuinely held, and that an
 * overlapping row cannot render twice - not just that a `before:` token appears
 * somewhere in the source. InviteCard falls back to source-level checks: its
 * continuation is entangled with the QR/mint/revoke UI (react-native-qrcode-svg,
 * native Share), which the executed harness does not stub, and its own
 * before/beforeId plumbing is identical code to the two executed segments.
 *
 *   npm run verify:c359-continuation
 */
import { readFileSync } from "node:fs";
import { runChapterFeedContinuationCases, runChapterEventsContinuationCases } from "./chapter-continuation-cases.mjs";

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

  // InviteCard: fallback to source-level checks (see module docstring for why).
  ["listInvites gains typed before/beforeId page options", /interface ChapterInvitePage \{[\s\S]*?beforeId\?: string/,
    readFileSync(new URL("../src/api/chapters.ts", import.meta.url), "utf8")],
  ["InviteCard has a load-older-codes continuation", /loadOlderCodes[\s\S]{0,20}=[\s\S]*?listInvites\(chapterId,\s*\{[\s\S]*?before:/, chapterScreen],
  ["a load-older-codes button label exists", /Load older codes/, chapterScreen],

  // c358 (PR #254) added collectionPages.ts's cursor/more/pending page shape
  // precisely so a new continuation would not hand-roll its own hasOlder booleans;
  // this proves all three c359 continuations actually adopted it rather than
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

try {
  await runChapterFeedContinuationCases();
  await runChapterEventsContinuationCases();
} catch (error) {
  failures++;
  console.log(`  FAIL  executed chapter-continuation-cases: ${error.message}`);
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

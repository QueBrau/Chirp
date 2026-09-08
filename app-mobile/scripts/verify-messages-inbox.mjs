/**
 * Offline regression guard for the mobile half of the bounded inbox (board c344).
 *
 * Source-level, syntax-anchored checks (verify-messages-pagination.mjs's style):
 * requires no network, auth, or database. Three things must stay true, each because
 * a plausible future edit could silently undo it:
 *
 *   1. messages/index.tsx's load path must never call listMessages( again — that
 *      per-row fanout (one history fetch per conversation, every load/refresh) is
 *      the literal thing this card removed.
 *   2. api/messages.ts's ConversationOut must keep carrying has_messages — the
 *      field the inbox preview now reads instead of fetching history.
 *   3. messages/[id].tsx must call leaveConversation( — the leave action this
 *      card wires up to a route that already existed but nothing called.
 *
 *   npm run verify:messages-inbox
 */

import { readFileSync } from "node:fs";

const indexSource = readFileSync(
  new URL("../app/(tabs)/messages/index.tsx", import.meta.url),
  "utf8",
);
const apiSource = readFileSync(new URL("../src/api/messages.ts", import.meta.url), "utf8");
const threadSource = readFileSync(
  new URL("../app/(tabs)/messages/[id].tsx", import.meta.url),
  "utf8",
);

let failures = 0;
const fail = (msg) => {
  console.error(`FAIL  ${msg}`);
  failures++;
};
const pass = (msg) => console.log(`PASS  ${msg}`);

// 1. The per-row fanout is gone from index.tsx entirely. Not "not awaited inside
// Promise.all" — the whole point is this screen no longer needs message history
// at all to render a preview, so the call should not appear anywhere in the file.
if (indexSource.includes("listMessages(")) {
  fail("messages/index.tsx still calls listMessages( — the per-row history fanout must be gone");
} else {
  pass("messages/index.tsx never calls listMessages(");
}

// 2. The inbox preview is derived from has_messages, not from a fetched history array.
if (!indexSource.includes(".has_messages")) {
  fail("messages/index.tsx never reads .has_messages — the preview must come from the summary field");
} else {
  pass("messages/index.tsx reads .has_messages for the preview");
}

// 3. ConversationOut carries the two new summary fields (property syntax, not just
// mentioned in a comment).
for (const [name, needle] of [
  ["has_messages field", "has_messages: boolean"],
  ["last_message_at field", "last_message_at: string | null"],
]) {
  if (!apiSource.includes(needle)) {
    fail(`api/messages.ts ConversationOut missing ${name}: ${JSON.stringify(needle)}`);
  } else {
    pass(`api/messages.ts ConversationOut has ${name}`);
  }
}

// 4. getConversation exists (the [id].tsx regression fix for a bounded inbox list).
if (!apiSource.includes("export async function getConversation(")) {
  fail("api/messages.ts is missing export async function getConversation(");
} else {
  pass("api/messages.ts exports getConversation(");
}

// 5. [id].tsx resolves its conversation through getConversation, not by fetching
// and searching the whole (now-bounded) list.
if (threadSource.includes("listConversations()")) {
  fail("messages/[id].tsx still calls listConversations() — use getConversation(id) instead");
} else {
  pass("messages/[id].tsx does not call listConversations()");
}
if (!threadSource.includes("getConversation(id)")) {
  fail("messages/[id].tsx never calls getConversation(id)");
} else {
  pass("messages/[id].tsx calls getConversation(id)");
}

// 6. The leave action is wired up — leaveConversation already existed unused
// before this card; this is the regression guard that keeps a future edit from
// quietly dropping the call site again.
if (!threadSource.includes("leaveConversation(")) {
  fail("messages/[id].tsx never calls leaveConversation( — the Leave action must be wired up");
} else {
  pass("messages/[id].tsx calls leaveConversation(");
}

if (failures > 0) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("ALL PASS");

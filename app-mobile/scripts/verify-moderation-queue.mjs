/**
 * Verifies the c353 moderation queue fix, from the real source.
 *
 *   npm run verify:moderation-queue
 *
 * The bug: `moderation.tsx` derived BOTH its pagination cursor and its "All clear"
 * empty state from the mutable `reports` display array. Resolving every visible
 * report drove that array to `[]`, which simultaneously bricked pagination (the
 * cursor source was gone) and rendered "All clear" while a full page of open
 * reports still sat behind it server-side. The fix moves cursor/exhaustion/tombstones
 * onto a `CollectionPage` held in a ref (`@/lib/collectionPages`, shared with
 * secretary.tsx/CommentsSheet.tsx), independent of the display array.
 *
 * These are source assertions for the wiring the executed cases below cannot observe
 * (no rendered JSX tree is captured, so which JSX branch is literally chosen is
 * checked by regex) plus the executed component cases, which drive the real handlers
 * against a controllable API boundary. Anchors are SYNTAX, never prose — c312's order
 * check matched a comment containing the phrase it was looking for and failed on
 * correct code.
 */
import { readFileSync } from "node:fs";
import { runModerationQueueCases } from "./moderation-queue-cases.mjs";

const MODERATION = new URL("../app/(tabs)/chapter/moderation.tsx", import.meta.url);
const COLLECTION_PAGES = new URL("../src/lib/collectionPages.ts", import.meta.url);

const moderation = readFileSync(MODERATION, "utf8");
const collectionPages = readFileSync(COLLECTION_PAGES, "utf8");

let failures = 0;
const check = (name, actual, expected = true) => {
  if (actual === expected) {
    console.log(`  PASS  ${name}`);
  } else {
    console.log(`  FAIL  ${name}`);
    console.log(`        expected ${JSON.stringify(expected)}`);
    console.log(`        got      ${JSON.stringify(actual)}`);
    failures++;
  }
};

console.log("\n-- moderation.tsx wiring --");

check(
  "imports the shared collection-page helpers, not a hand-rolled cursor",
  /import\s*\{\s*acceptPage,\s*beginOlderPage,\s*collectionPage,\s*mergePageRows,\s*pageExhausted,\s*reportOrder,\s*type CollectionPage,?\s*\}\s*from\s*"@\/lib\/collectionPages";/
    .test(moderation),
);
check(
  "closeReport tombstones the resolved id before it can race a straggling page merge",
  /pageRef\.current\.removed\.add\(reportId\)/.test(moderation),
);
check(
  "allClear requires exhaustion, never a bare empty array",
  /const allClear = reports\.length === 0 && pageExhausted\(pageRef\.current\)/.test(moderation),
);
check(
  "needsRefill is gated on both the exhaustion flag and the threshold",
  /const needsRefill = hasOlder && !loadingOlder && reports\.length < REFILL_THRESHOLD/.test(moderation),
);
check(
  "the Screen is wired to pull-to-refresh",
  /<Screen[^>]*\bonRefresh=\{load\}/.test(moderation),
);

// Order is the whole fix: if the bare `reports.length === 0` branch is reached before
// the `allClear` branch, an emptied-but-not-exhausted page renders "All clear" again
// even with every other piece of this fix present.
const allClearAt = moderation.indexOf("allClear ? (");
const bareEmptyAt = moderation.indexOf("reports.length === 0 ? (\n        <EmptyState title=\"Loading more reports\"");
check(
  "the allClear branch is checked BEFORE the bare empty-array fallback",
  allClearAt !== -1 && bareEmptyAt !== -1 && allClearAt < bareEmptyAt,
);

console.log("\n-- collectionPages.ts additive exports --");

check("exports reportOrder", /export const reportOrder = /.test(collectionPages));
check("exports pageExhausted", /export function pageExhausted\(/.test(collectionPages));

console.log("\n-- executed component cases --");
await runModerationQueueCases();

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

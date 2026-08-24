/**
 * Offline regression guard for the mobile half of message cursor pagination.
 *
 * The backend already accepts `before_id` as the tie-break alongside `before`.
 * This source-level check keeps a future client edit from silently dropping that
 * field again while requiring no network, auth, or database.
 *
 *   npm run verify:messages-pagination
 */

import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/api/messages.ts", import.meta.url), "utf8");

const required = [
  ["typed cursor option", "before_id?: string"],
  ["cursor request field", "before_id: options.before_id"],
  ["exported options type", "export interface ListMessagesOptions"],
];

for (const [name, needle] of required) {
  if (!source.includes(needle)) {
    console.error(`FAIL  ${name}: missing ${JSON.stringify(needle)}`);
    process.exit(1);
  }
  console.log(`PASS  ${name}`);
}

if (source.includes("options: { before?: string; limit?: number }")) {
  console.error("FAIL  listMessages still uses the pre-before_id inline options type");
  process.exit(1);
}

console.log("ALL PASS");

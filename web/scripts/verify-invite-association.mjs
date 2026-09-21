/** c5: check actual Vite output and the Firebase routes that serve it. */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const read = path => readFileSync(resolve(root, path));
const json = path => JSON.parse(read(path).toString("utf8"));
const { hosting } = json("firebase.json");
const app = json("../app-mobile/app.json").expo;

assert.equal(hosting.public, "dist", "publish only the web build output");
assert.equal(hosting.appAssociation, "NONE", "Firebase must serve our association, not generate an empty one");
assert.deepEqual(hosting.ignore, ["firebase.json", "**/.*", "**/node_modules/**"],
  "keep hidden files excluded; association sources deliberately have visible names");
assert.equal((hosting.redirects ?? []).length, 0,
  "new redirects need review: they take precedence over association static files and rewrites");

for (const [index, name] of ["apple-app-site-association", "assetlinks.json"].entries()) {
  assert.ok(read(`public/${name}`).equals(read(`dist/${name}`)), `${name} must survive the real Vite build unchanged`);
  assert.deepEqual(hosting.rewrites[index], {
    source: `/.well-known/${name}`, destination: `/${name}`,
  }, `${name} needs an exact rewrite before the SPA fallback, not an HTTP redirect`);
  const headers = hosting.headers.find(row => row.source === `/.well-known/${name}`)?.headers;
  assert.ok(headers?.some(h => h.key.toLowerCase() === "content-type" && h.value === "application/json"),
    `${name} needs JSON headers on the original request path`);
}
assert.ok(hosting.headers.some(row => row.source === "/apple-app-site-association"
  && row.headers.some(h => h.key.toLowerCase() === "content-type" && h.value === "application/json")));
assert.deepEqual(json("dist/apple-app-site-association"), {
  applinks: { apps: [], details: [{ appID: `7G7PL87H62.${app.ios.bundleIdentifier}`, paths: ["/join-chapter"] }] },
}, "associate only the reviewed team/bundle and invite path; never capture Stripe or legal routes");
// No verified Android release certificate is available yet. An empty association
// preserves the current hosted response and cannot claim a fabricated fingerprint.
assert.deepEqual(json("dist/assetlinks.json"), [], "Android association awaits the actual signing certificate");
assert.ok(readdirSync(resolve(root, "dist")).includes("index.html"), "the browser fallback must remain deployed");
console.log("PASS: built iOS association and explicit pending Android association; exact Firebase routes and JSON headers");

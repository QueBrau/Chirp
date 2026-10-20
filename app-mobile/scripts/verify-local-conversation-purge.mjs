/** Exercise the exported purgeConversationData helper against Node's real SQLite engine. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("../src/db/schema.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const module = { exports: {} };
runInNewContext(
  `((require, module, exports) => { ${js}\n})(require, module, module.exports);`,
  {
    require: (name) => {
      if (name === "expo-sqlite") return { openDatabaseAsync: async () => {} };
      if (name === "react-native") return { Platform: { OS: "ios" } };
      throw new Error(`Unexpected module ${name}`);
    },
    module,
  },
);
const { purgeConversationData } = module.exports;
assert.equal(typeof purgeConversationData, "function");

const sqlite = new DatabaseSync(":memory:");
sqlite.exec(`
  CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, body TEXT NOT NULL);
  CREATE TABLE outbox (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, plaintext TEXT NOT NULL);
  INSERT INTO messages VALUES ('m1', 'conv-a', 'private a'), ('m2', 'conv-b', 'keep b');
  INSERT INTO outbox VALUES ('o1', 'conv-a', 'queued a'), ('o2', 'conv-b', 'keep queued b');
`);

let failOnOutboxDelete = true;
const db = {
  async withExclusiveTransactionAsync(task) {
    sqlite.exec("BEGIN EXCLUSIVE");
    const tx = {
      async runAsync(sql, ...params) {
        if (failOnOutboxDelete && sql.startsWith("DELETE FROM outbox")) {
          failOnOutboxDelete = false;
          throw new Error("injected failure after first delete");
        }
        sqlite.prepare(sql).run(...params);
      },
    };
    try {
      await task(tx);
      sqlite.exec("COMMIT");
    } catch (error) {
      sqlite.exec("ROLLBACK");
      throw error;
    }
  },
};

await assert.rejects(purgeConversationData(db, "conv-a"), /injected failure/);
const ids = (table) => sqlite.prepare(`SELECT id FROM ${table} ORDER BY id`).all().map((row) => row.id);
assert.deepEqual(ids("messages"), ["m1", "m2"]);
assert.deepEqual(ids("outbox"), ["o1", "o2"]);

await purgeConversationData(db, "conv-a");
await purgeConversationData(db, "conv-a");
assert.deepEqual(ids("messages"), ["m2"]);
assert.deepEqual(ids("outbox"), ["o2"]);
await assert.rejects(purgeConversationData(db, ""), /Invalid conversation id/);
sqlite.close();
console.log("PASS local conversation purge helper: rollback, isolation, idempotence, invalid input");

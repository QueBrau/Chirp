/** Execute production payment error copy and the shared map without native APIs. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; this.detail = detail; }
}
const load = (path, dependencies) => {
  const code = ts.transpileModule(readFileSync(new URL(`../${path}`, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(`(function(require,module,exports){${code}\n})`)(name => {
    if (!(name in dependencies)) throw new Error(`Unstubbed dependency: ${name}`);
    return dependencies[name];
  }, module, module.exports);
  return module.exports;
};
const alert = load("src/lib/alert.ts", {
  "react-native": { Alert: {}, Platform: { OS: "ios" } },
  "@/api/client": { ApiError },
  "@/api/operation": { operationErrorMessage: () => null },
});
const { duesPaymentError } = load("src/payments/errors.ts", {
  "@/api/client": { ApiError }, "@/lib/alert": alert,
});

for (const [detail, title, action] of [
  ["payment_outcome_unconfirmed", "Payment not confirmed", /Retry with the same payment method/],
  ["payment_reconciliation_required", "Payment needs review", /Contact your treasurer before starting another payment/],
  ["payment_provider_rejected", "Payment unavailable", /Contact your treasurer/],
  ["payment_already_in_progress", "Payment in progress", /already in progress/],
]) {
  const copy = duesPaymentError(new ApiError(detail === "payment_outcome_unconfirmed" ? 503 : 409, detail));
  assert.equal(copy.title, title);
  assert.match(copy.message, action);
  assert.doesNotMatch(copy.message, /payment_(?:outcome|reconciliation|provider|already)/);
  assert.doesNotMatch(copy.title, /failed/i);
}
for (const error of [new ApiError(500, "SECRET provider payload"), new Error("SECRET provider payload")]) {
  const copy = duesPaymentError(error);
  assert.equal(copy.title, "Payment not confirmed");
  assert.match(copy.message, /same payment method/);
  assert.doesNotMatch(JSON.stringify(copy), /SECRET/);
}
console.log("PASS c366: 6 executed payment uncertainty/status/copy cases");

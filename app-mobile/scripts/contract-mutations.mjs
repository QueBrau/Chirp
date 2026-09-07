/** Real-source contract faults, applied only in each verifier child's memory. */
const replace = (before, after) => source => source.replace(before, after);
const csvRoute = "`/chapters/${chapterId}/ledger/export.csv`";

export const MUTATIONS = {
  "csv-route": {
    file: "/src/api/finance.ts",
    source: replace(csvRoute, "`/chapters/${chapterId}/ledger/missing.csv`"),
    expected: "no backend route",
  },
  "aliased-csv-route": {
    file: "/src/api/finance.ts",
    source: source => source.replace("requestText }", "requestText as csv }")
      .replace("return requestText(", "return csv(")
      .replace(csvRoute, "`/chapters/${chapterId}/ledger/missing.csv`"),
    expected: "no backend route",
  },
  "unresolved-csv-path": {
    file: "/src/api/finance.ts",
    source: replace(csvRoute, "String(chapterId)"),
    expected: "UNRESOLVED finance.ts:",
  },
  "unresolved-method": {
    file: "/src/api/feed.ts",
    source: replace('method: "POST", body', 'method: String("POST"), body'),
    expected: "unresolved/overridable HTTP method",
  },
  "hidden-query-spread": {
    file: "/src/api/feed.ts",
    source: replace('method: "POST", body', '...{ query: { wrong_key: 1 } }, method: "POST", body'),
    expected: "unresolved/overridable HTTP method",
  },
  "shorthand-method": {
    file: "/src/api/feed.ts",
    source: source => source.replace('import { request, requestWithHeaders } from "./client";',
      'import { request, requestWithHeaders } from "./client";\nconst method = "POST" as const;')
      .replace("query: { limit: opts.limit", "method, query: { limit: opts.limit"),
    expected: "unresolved/overridable HTTP method",
  },
  "router-local-response": {
    file: "/src/api/chirps.ts",
    source: replace("my_vote: number | null;", "required_vote_that_server_never_sends: number | null;"),
    expected: "response field absent from backend required_vote_that_server_never_sends",
  },
  "wrong-query-field": {
    file: "/src/api/feed.ts",
    source: replace("query: { limit: opts.limit", "query: { wrong_limit: opts.limit"),
    expected: "unknown query field wrong_limit",
  },
  "wrong-response-field": {
    file: "/src/api/feed.ts",
    source: replace("display_name: string;", "wrong_display_name: string;"),
    expected: "response field absent from backend wrong_display_name",
  },
  "missing-server-response-field": {
    inventory: inventory => { delete inventory.schemas.FeedPostOut.fields.display_name; },
    expected: "response field absent from backend display_name",
  },
  "wrong-event-field": {
    file: "/src/realtime/socket.ts",
    source: replace("message_id: string;", "wrong_message_id: string;"),
    expected: "WebSocket field absent from backend wrong_message_id",
  },
  "missing-server-event-field": {
    inventory: inventory => {
      inventory.events.message = inventory.events.message.map(fields => fields.filter(field => field !== "message_id"));
    },
    expected: "WebSocket field absent from backend message_id",
  },
};

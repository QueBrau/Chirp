/**
 * Verifies the chart maths and renders a preview of it.
 *
 *   npm run verify:charts
 *
 * app-mobile has no test runner, and `tsc` cannot tell you that an arc is drawn at
 * the wrong angle — chart geometry fails by rendering a confident, wrong picture
 * rather than by throwing. So the two pure modules behind the treasurer charts
 * (components/charts/geometry.ts and lib/treasury.ts) are compiled on their own and
 * exercised here, then drawn into .chart-verify/charts-preview.html so the result
 * can be LOOKED at in both colour modes, which is the half no assertion covers.
 */
import { writeFileSync, mkdirSync } from "node:fs";

// The compiled output is ESM while app-mobile's package.json is not a module (and
// must not become one — Metro depends on that). A type marker beside the build
// scopes the module system to this directory instead. Written before the dynamic
// imports below, which is why they are dynamic.
const OUT = new URL("../.chart-verify/", import.meta.url);
mkdirSync(OUT, { recursive: true });
writeFileSync(new URL("./package.json", OUT), '{"type":"module"}\n');

const { donutSegments, trendGeometry } = await import("../.chart-verify/components/charts/geometry.js");
const { runningBalance, spendByCategory, duesProgress, inOutTotals, assignCategorySlots } =
  await import("../.chart-verify/lib/treasury.js");

let failures = 0;
const check = (name, cond, extra = "") => {
  if (cond) { console.log(`  PASS  ${name}`); }
  else { console.log(`  FAIL  ${name} ${extra}`); failures++; }
};
const near = (a, b, eps = 1e-6) => Math.abs(a - b) < eps;

console.log("GEOMETRY — donut");
{
  const segs = donutSegments([50, 30, 20], { size: 160, thickness: 34 });
  check("three values -> three segments", segs.length === 3);
  check("fractions sum to 1", near(segs.reduce((s, x) => s + x.fraction, 0), 1));
  check("every path is closed", segs.every((s) => s.d.trim().endsWith("Z")));
  check("no NaN in any path", segs.every((s) => !/NaN/.test(s.d)));

  const withJunk = donutSegments([10, 0, -5, 90], { size: 160, thickness: 34 });
  check("zero and negative values dropped", withJunk.length === 2);

  const solo = donutSegments([42], { size: 160, thickness: 34 });
  check("single 100% slice renders a full ring, not a blank", solo.length === 1 && solo[0].d.split("M").length === 3);

  const tiny = donutSegments([9999, 1], { size: 160, thickness: 34 });
  check("a sliver still draws rather than vanishing", tiny.length === 2 && !/NaN/.test(tiny[1].d));

  check("empty input -> no segments", donutSegments([], { size: 160, thickness: 34 }).length === 0);
  check("all-zero input -> no segments", donutSegments([0, 0], { size: 160, thickness: 34 }).length === 0);
}

console.log("GEOMETRY — trend");
{
  const g = trendGeometry([{ x: 0, y: 10 }, { x: 1, y: 30 }, { x: 2, y: 20 }], { width: 300, height: 100, inset: 6 });
  check("line starts with a moveto", g.line.startsWith("M"));
  check("area is closed", g.area.endsWith("Z"));
  check("end dot is the last point", g.last !== null);
  check("all-positive series has no zero line", g.zeroY === null);

  const crossing = trendGeometry([{ x: 0, y: 100 }, { x: 1, y: -100 }], { width: 300, height: 100 });
  check("series crossing zero exposes a zero line", crossing.zeroY !== null);
  check("area closes onto zero, not the floor", crossing.zeroY === crossing.baselineY);

  const flat = trendGeometry([{ x: 0, y: 5 }, { x: 1, y: 5 }], { width: 300, height: 100 });
  check("flat series does not divide by zero", !/NaN/.test(flat.line));
  check("empty series is empty, not broken", trendGeometry([], { width: 300, height: 100 }).line === "");
  const one = trendGeometry([{ x: 0, y: 5 }], { width: 300, height: 100 });
  check("single point still produces a path", one.line.startsWith("M") && !/NaN/.test(one.line));
}

console.log("TREASURY");
const entry = (amount, category, iso, type = amount >= 0 ? "dues_payment" : "expense", cycle = null) => ({
  id: Math.random().toString(36).slice(2), chapter_id: "c", entry_type: type,
  amount_cents: amount, category, description: null, related_user_id: null,
  dues_cycle_id: cycle, stripe_payment_intent_id: null, corrects_entry_id: null,
  created_by: "u", created_at: iso,
});
{
  const shuffled = [
    entry(-2000, "formal", "2026-03-02T00:00:00Z"),
    entry(10000, "dues", "2026-01-01T00:00:00Z"),
    entry(-3000, "rush", "2026-02-01T00:00:00Z"),
  ];
  const bal = runningBalance(shuffled);
  check("running balance sorts chronologically regardless of input order",
    bal.map((p) => p.y).join(",") === "10000,7000,5000", bal.map((p) => p.y).join(","));

  const cats = spendByCategory([
    entry(-5000, "formal", "2026-01-01T00:00:00Z"),
    entry(-3000, "rush", "2026-01-02T00:00:00Z"),
    entry(90000, "dues", "2026-01-03T00:00:00Z"),
    entry(-1000, null, "2026-01-04T00:00:00Z"),
  ]);
  check("income excluded from spend breakdown", !cats.some((c) => c.label === "dues"));
  check("largest category first", cats[0].label === "formal");
  check("null category labelled, not dropped", cats.some((c) => c.label === "Uncategorised"));
  check("magnitudes are positive", cats.every((c) => c.cents > 0));

  const many = spendByCategory(
    ["a", "b", "c", "d", "e", "f", "g"].map((c, i) => entry(-(1000 - i * 10), c, `2026-01-0${i + 1}T00:00:00Z`)),
  );
  check("folds past five into one Other", many.length === 6 && many[5].isOther, JSON.stringify(many.map(m => m.label)));
  check("Other carries the summed tail", many[5].cents === (1000 - 50) + (1000 - 60));

  const cycle = { id: "cy1", chapter_id: "c", name: "Spring", amount_cents: 10000, due_date: "2026-04-01", created_at: "2026-01-01T00:00:00Z" };
  const paid = [entry(10000, "dues", "2026-02-01T00:00:00Z", "dues_payment", "cy1"),
                entry(10000, "dues", "2026-02-02T00:00:00Z", "dues_payment", "cy1")];
  const prog = duesProgress(cycle, paid, 4);
  check("dues fraction is collected/expected", near(prog.fraction, 0.5), String(prog.fraction));
  check("paid count counts payments on this cycle", prog.paidCount === 2);
  check("no cycle -> null, not a fake meter", duesProgress(undefined, paid, 4) === null);
  const over = duesProgress(cycle, [...paid, ...paid, ...paid], 4);
  check("over-collection clamps the meter but is flagged", over.fraction === 1 && over.overCollected);
  const noRoster = duesProgress(cycle, paid, 0);
  check("empty roster does not divide by zero", noRoster.fraction === 0);

  const io = inOutTotals([entry(500, "a", "2026-01-01T00:00:00Z"), entry(-200, "b", "2026-01-02T00:00:00Z")]);
  check("in/out totals both positive", io.inCents === 500 && io.outCents === 200);
}

console.log("TREASURY — stable colour slots");
{
  const labels = ["formal", "rush", "house", "philanthropy", "letters"];
  const a = assignCategorySlots(labels, 5);
  const b = assignCategorySlots([...labels].reverse(), 5);
  check("assignment does not depend on input order",
    labels.every((l) => a.get(l) === b.get(l)), JSON.stringify([...a]));
  check("every label gets a slot", labels.every((l) => typeof a.get(l) === "number"));
  check("slots are unique", new Set([...a.values()]).size === labels.length);
  check("slots stay in range", [...a.values()].every((v) => v >= 0 && v < 5));

  // The property that matters: amounts moving must not repaint anything.
  const before = spendByCategory([
    entry(-9000, "formal", "2026-01-01T00:00:00Z"),
    entry(-1000, "rush", "2026-01-02T00:00:00Z"),
  ]);
  const after = spendByCategory([
    entry(-1000, "formal", "2026-01-01T00:00:00Z"),
    entry(-9000, "rush", "2026-01-02T00:00:00Z"),
  ]);
  check("rank flip changes ORDER", before[0].label !== after[0].label);
  const sa = assignCategorySlots(before.map((s) => s.label), 5);
  const sb = assignCategorySlots(after.map((s) => s.label), 5);
  check("rank flip does NOT change colour", sa.get("formal") === sb.get("formal") && sa.get("rush") === sb.get("rush"));
  check("zero slots is handled, not divided by", assignCategorySlots(["a"], 0).size === 0);
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);

// ---- render a real preview so the picture can be looked at, not just asserted ----
const LIGHT = { surface:"#FFFFFF", bg:"#F6F7FB", ink:"#101223", inkSecondary:"#575B75", inkFaint:"#9BA0B8",
  border:"rgba(16,18,35,0.08)", accent:"#5B5BF6", surfaceAlt:"#EFF1F7", success:"#17A673", danger:"#E5484D",
  cat:["#5B5BF6","#DB2777","#0284C7","#EA580C","#0D9488"], other:"#9BA0B8", accentSoft:"#ECECFE" };
const DARK = { surface:"#15161F", bg:"#0C0D14", ink:"#F2F3FA", inkSecondary:"#A6AAC4", inkFaint:"#666B85",
  border:"rgba(242,243,250,0.09)", accent:"#7C7CFF", surfaceAlt:"#1D1E2A", success:"#2BD597", danger:"#FF6369",
  cat:["#7C7CFF","#EC4899","#0891B2","#EA580C","#10A99A"], other:"#666B85", accentSoft:"rgba(124,124,255,0.16)" };

const demo = [
  entry(240000, "dues", "2026-01-10T00:00:00Z", "dues_payment", "cy1"),
  entry(-48000, "rush", "2026-01-24T00:00:00Z"),
  entry(-96000, "formal", "2026-02-08T00:00:00Z"),
  entry(120000, "dues", "2026-02-14T00:00:00Z", "dues_payment", "cy1"),
  entry(-31000, "house", "2026-02-27T00:00:00Z"),
  entry(-22000, "philanthropy", "2026-03-09T00:00:00Z"),
  entry(-14500, "letters", "2026-03-18T00:00:00Z"),
  entry(60000, "dues", "2026-03-25T00:00:00Z", "dues_payment", "cy1"),
  entry(-9000, "socials", "2026-04-02T00:00:00Z"),
];
const usd = (c) => (c/100).toLocaleString("en-US",{style:"currency",currency:"USD",maximumFractionDigits:0});
const slices = spendByCategory(demo);
const points = runningBalance(demo);

function panel(P, label) {
  const W = 320, H = 110, INSET = 8;
  const t = trendGeometry(points.map((p,i)=>({x:i,y:p.y})), { width: W, height: H, inset: INSET });
  const segs = donutSegments(slices.map(s=>s.cents), { size: 168, thickness: 36 });
  const slots = assignCategorySlots(slices.filter(s=>!s.isOther).map(s=>s.label), P.cat.length);
  const colour = (s) => s.isOther ? P.other : P.cat[slots.get(s.label) ?? 0];
  const total = slices.reduce((a,b)=>a+b.cents,0);
  const prog = duesProgress({id:"cy1",amount_cents:10000,chapter_id:"c",name:"Spring",due_date:"2026-04-01",created_at:"x"}, demo, 52);
  const balance = points[points.length-1].y;
  return `
  <section style="background:${P.bg};padding:20px;border-radius:24px">
    <div style="font:600 11px/1 system-ui;letter-spacing:.08em;text-transform:uppercase;color:${P.inkFaint};margin-bottom:10px">${label}</div>

    <div style="background:linear-gradient(135deg,#6366F1,#8B5CF6);border-radius:20px;padding:18px;margin-bottom:12px">
      <div style="font:600 11px/1 system-ui;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.8)">Chapter balance</div>
      <div style="font:800 30px/1.2 system-ui;font-variant-numeric:tabular-nums;color:#fff;margin-top:6px">${usd(balance)}</div>
    </div>

    <div style="background:${P.surface};border:1px solid ${P.border};border-radius:20px;padding:16px;margin-bottom:12px">
      <div style="font:700 16px/1 system-ui;color:${P.ink}">Balance over time</div>
      <div style="font:500 12.5px/1.4 system-ui;color:${P.inkSecondary};margin:4px 0 12px">9 entries · running total, oldest to newest</div>
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
        ${t.zeroY!==null?`<line x1="0" y1="${t.zeroY}" x2="${W}" y2="${t.zeroY}" stroke="${P.border}" stroke-width="1"/>`:""}
        <path d="${t.area}" fill="${P.accent}" fill-opacity="0.10"/>
        <path d="${t.line}" fill="none" stroke="${P.accent}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${t.last.x}" cy="${t.last.y}" r="4.5" fill="${P.accent}" stroke="${P.surface}" stroke-width="2"/>
      </svg>
      <div style="font:500 12.5px/1 system-ui;font-variant-numeric:tabular-nums;color:${P.inkFaint};margin-top:6px">
        Low ${usd(t.min)} · High ${usd(t.max)}
      </div>
    </div>

    <div style="background:${P.surface};border:1px solid ${P.border};border-radius:20px;padding:16px;margin-bottom:12px">
      <div style="font:700 16px/1 system-ui;color:${P.ink}">Dues collected</div>
      <div style="font:500 12.5px/1.4 system-ui;color:${P.inkSecondary};margin:4px 0 12px">Spring · ${prog.paidCount} of ${prog.memberCount} members paid</div>
      <div style="height:12px;border-radius:999px;background:${P.accentSoft};overflow:hidden">
        <div style="height:100%;width:${(prog.fraction*100).toFixed(1)}%;background:${P.accent};border-radius:999px"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font:800 17px/1.6 system-ui;font-variant-numeric:tabular-nums;color:${P.ink}">
        <span>${usd(prog.collectedCents)}</span><span style="color:${P.inkFaint};font-weight:500;font-size:12.5px">of ${usd(prog.expectedCents)}</span>
      </div>
    </div>

    <div style="background:${P.surface};border:1px solid ${P.border};border-radius:20px;padding:16px">
      <div style="font:700 16px/1 system-ui;color:${P.ink}">Where the money went</div>
      <div style="font:500 12.5px/1.4 system-ui;color:${P.inkSecondary};margin:4px 0 12px">${usd(total)} out across ${slices.length} categories</div>
      <div style="display:flex;gap:16px;align-items:center">
        <svg width="168" height="168" viewBox="0 0 168 168" style="flex:none">
          ${segs.map((s,i)=>`<path d="${s.d}" fill="${colour(slices[i])}" fill-rule="evenodd"/>`).join("")}
          <text x="84" y="80" text-anchor="middle" style="font:800 17px system-ui;font-variant-numeric:tabular-nums;fill:${P.ink}">${usd(total)}</text>
          <text x="84" y="96" text-anchor="middle" style="font:500 11px system-ui;fill:${P.inkFaint}">total out</text>
        </svg>
        <div style="flex:1;display:flex;flex-direction:column;gap:8px">
          ${slices.map((s)=>`<div style="display:flex;align-items:center;gap:8px">
            <span style="width:10px;height:10px;border-radius:3px;background:${colour(s)};flex:none"></span>
            <span style="flex:1;font:500 12.5px/1 system-ui;color:${P.inkSecondary};text-transform:capitalize">${s.label}</span>
            <span style="font:800 13px/1 system-ui;font-variant-numeric:tabular-nums;color:${P.ink}">${usd(s.cents)}</span>
          </div>`).join("")}
        </div>
      </div>
    </div>
  </section>`;
}

writeFileSync(new URL("../.chart-verify/charts-preview.html", import.meta.url),
`<!doctype html><meta charset="utf-8"><title>Chirp treasurer charts</title>
<body style="margin:0;padding:24px;background:#8b8b96;font-family:system-ui">
<div style="display:flex;gap:24px;align-items:flex-start;max-width:1100px">
${panel(LIGHT,"Light mode")}${panel(DARK,"Dark mode")}
</div></body>`);
console.log("preview written: charts-preview.html");

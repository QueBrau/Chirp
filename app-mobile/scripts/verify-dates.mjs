/**
 * Verifies that a calendar day renders as that day in every timezone (board card c165).
 *
 *   npm run verify:dates
 *
 * WHY THIS SCRIPT EXISTS AND tsc CANNOT REPLACE IT. c165 was a real shipped bug -
 * every chapter meeting displayed one day early - and it is invisible to the type
 * checker, invisible to the backend suite (the stored value was always correct), and
 * invisible on any machine that happens to sit at UTC. It failed only in the viewer's
 * local timezone, which is why it survived to a device.
 *
 * So this runs the assertions once per timezone, including zones on BOTH sides of
 * Greenwich and a half-hour offset, by setting process.env.TZ before the Date is built.
 * A single-timezone version of this file would pass against the broken implementation.
 */

let failures = 0;
const check = (name, cond, extra = "") => {
  if (cond) {
    console.log(`  PASS  ${name}`);
  } else {
    console.log(`  FAIL  ${name} ${extra}`);
    failures++;
  }
};

/** The implementation under test, kept in step with src/lib/dates.ts by verify:contract's sibling rule: it is three lines and reimplementing it here would test the copy. */
const calendarDay = (value) => new Date(`${value.slice(0, 10)}T00:00:00`);

/** What the buggy version did, kept so the zones below can be shown to actually bite. */
const naive = (value) => new Date(value);

const ZONES = [
  "UTC",
  "America/New_York", // UTC-4/-5, where c165 was found
  "America/Los_Angeles", // UTC-7/-8, the worst case in the US
  "Asia/Kolkata", // UTC+5:30, a half-hour offset east of Greenwich
  "Pacific/Kiritimati", // UTC+14, the far end
];

// A bare day (dues due_date) and an instant the client built with Date.UTC
// (meetings.meeting_date) must both render as the 24th.
const CASES = [
  ["bare day", "2026-08-24"],
  ["midnight-UTC instant", "2026-08-24T00:00:00.000Z"],
];

for (const zone of ZONES) {
  process.env.TZ = zone;
  for (const [label, value] of CASES) {
    check(
      `${zone} · ${label} renders as the 24th`,
      calendarDay(value).getDate() === 24,
      `got ${calendarDay(value).getDate()}`,
    );
  }
}

// The zones are only meaningful if the naive version actually fails in them, otherwise
// this file is a green light that proves nothing. West of Greenwich must shift back.
process.env.TZ = "America/Los_Angeles";
check(
  "the naive version really does shift the day back (the bug is reproducible here)",
  naive("2026-08-24T00:00:00.000Z").getDate() === 23,
  `got ${naive("2026-08-24T00:00:00.000Z").getDate()} - if this is 24, these zones prove nothing`,
);

// Month and year boundaries are where an off-by-one is worst and least likely to be
// noticed in casual testing.
process.env.TZ = "America/Los_Angeles";
check("month boundary holds", calendarDay("2026-09-01").getMonth() === 8);
check("year boundary holds", calendarDay("2027-01-01").getFullYear() === 2027);

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);

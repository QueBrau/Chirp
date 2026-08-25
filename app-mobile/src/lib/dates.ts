/**
 * Rendering a CALENDAR DAY as that day, in every timezone (board card c165).
 *
 * THE BUG THIS EXISTS TO PREVENT, because it is invisible on a machine sitting at UTC
 * and was found only by looking at a device in Eastern time: `new Date("2026-08-24")`
 * and `new Date("2026-08-24T00:00:00Z")` both parse as midnight UTC, and
 * toLocaleDateString then renders that INSTANT in the viewer's zone. Anywhere west of
 * Greenwich, midnight UTC is the previous evening, so the day shifts back by one. A
 * secretary logs Thursday's chapter meeting and the whole chapter reads Wednesday.
 *
 * The fix is to drop the instant entirely and rebuild the day as LOCAL midnight, which
 * is what the `T00:00:00` with NO trailing Z does. Slicing to the first ten characters
 * is lossless for our data: every date-only value on the wire is either a bare
 * YYYY-MM-DD (dues due_date) or an instant the client itself built with Date.UTC and
 * so is exactly midnight UTC (meetings.meeting_date, see parseMeetingDate).
 *
 * DO NOT ROUTE REAL TIMESTAMPS THROUGH HERE. A ledger entry's created_at or a message's
 * sent time is a genuine instant, and rendering it in the viewer's local zone is not a
 * bug, it is the entire point. treasurer.tsx's entryDate is deliberately left alone for
 * exactly this reason. The test for which one you have: would two people in different
 * timezones be right to disagree about it? A meeting date, no. A timestamp, yes.
 *
 * One definition rather than the four near-copies this replaced - dues.tsx,
 * treasurer.tsx, president.tsx and (missing it, which is how c165 happened)
 * secretary.tsx.
 */

/** A date-only value as a Date at LOCAL midnight, safe to format for display. */
export function calendarDay(value: string): Date {
  return new Date(`${value.slice(0, 10)}T00:00:00`);
}

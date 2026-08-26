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

/**
 * An event's start (and optional end) as a human-readable local string (board c198).
 *
 * DELIBERATELY NOT calendarDay(). Apply this file's own test to a party: would two
 * people in different timezones be right to disagree about when it starts? Yes - a
 * 9pm party in Greensboro is 6pm for someone reading from California, and rendering
 * the instant in the viewer's own zone is the correct answer, not a bug. events
 * .starts_at is a timestamptz precisely so this works; the free-text date_label it
 * replaced could not express an instant at all.
 *
 * The end is appended as a bare time when it falls on the same local day, and as a
 * full date otherwise - "7:00 PM - 2:00 AM" reads as one night out, while
 * "Sep 27, 7:00 PM - Sep 28, 2:00 AM" reads as an admin error until you look twice.
 */
export function eventWhen(startsAt: string, endsAt?: string | null): string {
  const start = new Date(startsAt);
  if (Number.isNaN(start.getTime())) return "Date to be announced";

  const dayPart = start.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  const timePart = start.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const opening = `${dayPart} - ${timePart}`;

  if (!endsAt) return opening;
  const end = new Date(endsAt);
  if (Number.isNaN(end.getTime())) return opening;

  const sameLocalDay = start.toDateString() === end.toDateString();
  const endText = sameLocalDay
    ? end.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
    : `${end.toLocaleDateString(undefined, { month: "short", day: "numeric" })}, ${end.toLocaleTimeString(
        undefined,
        { hour: "numeric", minute: "2-digit" },
      )}`;
  return `${opening} to ${endText}`;
}

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

/**
 * Compact relative age for card captions ("just now", "5m", "3h", "2d") - the one
 * scale Home's feed, an org's feed and the comments thread all caption their
 * timestamps with (board card c238; it was the same six lines in all three).
 *
 * DELIBERATELY NOT calendarDay(), exactly like eventWhen above. A post's created_at
 * is a real INSTANT, not a calendar day, and this reads it as elapsed milliseconds
 * from now, which carries no timezone opinion at all - "5m" is 5m everywhere.
 *
 * This is the MINUTE-scale formatter. The hour-scale copies in chirps/index.tsx and
 * chapter/moderation.tsx are deliberately NOT folded in here: they emit different
 * strings ("5h ago", not "5h"), so sharing them would need a suffix flag, and a flag
 * costs more than the duplication saves.
 */
export function compactAge(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

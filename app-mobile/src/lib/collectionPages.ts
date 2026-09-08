/** Cursor progress belongs to server pages, never to locally edited display rows. */
import type { PostCommentOut } from "@/api/feed";
import type { MeetingWithAttendance } from "@/api/meetings";
import type { ContentReportOut } from "@/api/moderation";
import type { PollOut } from "@/api/polls";

export interface CollectionCursor { before: string; beforeId: string }
export interface CollectionPage {
  cursor: CollectionCursor | null;
  more: boolean;
  pending: symbol | null;
  removed: Set<string>;
}

export function collectionPage(): CollectionPage {
  return { cursor: null, more: false, pending: null, removed: new Set() };
}

export function beginOlderPage(page: CollectionPage): symbol | null {
  if (page.pending !== null || !page.more || page.cursor === null) return null;
  const request = Symbol("page");
  page.pending = request;
  return request;
}

/** The caller supplies the boundary of this response in its server-defined order. */
export function acceptPage(page: CollectionPage, count: number, size: number, cursor: CollectionCursor | null): void {
  if (cursor !== null) page.cursor = cursor;
  page.more = count === size;
}

/** Names the invariant this module exists to enforce: exhaustion is a property of
 * the last fetched SERVER page, never inferred from a display array going empty. */
export function pageExhausted(page: CollectionPage): boolean {
  return !page.more;
}

const compareId = (a: string, b: string) => a === b ? 0 : a < b ? -1 : 1;
// PostgreSQL preserves microseconds; Date.parse alone truncates them and can
// incorrectly use the UUID tie-break for two different instants in one millisecond.
const submillisecond = (value: string) =>
  Number((/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] ?? "").padEnd(6, "0").slice(3, 6));
const newest = (aTime: string, aId: string, bTime: string, bId: string) =>
  Date.parse(bTime) - Date.parse(aTime) || submillisecond(bTime) - submillisecond(aTime) || compareId(bId, aId);

export const meetingOrder = (a: MeetingWithAttendance, b: MeetingWithAttendance) =>
  newest(a.meeting.meeting_date, a.meeting.id, b.meeting.meeting_date, b.meeting.id);
export const pollOrder = (a: PollOut, b: PollOut) => newest(a.created_at, a.id, b.created_at, b.id);
export const commentOrder = (a: PostCommentOut, b: PostCommentOut) => -newest(a.created_at, a.id, b.created_at, b.id);
export const reportOrder = (a: ContentReportOut, b: ContentReportOut) =>
  newest(a.created_at, a.id, b.created_at, b.id);

/** Pure functional merge: a delayed page cannot overwrite newer displayed edits. */
export function mergePageRows<T>(
  current: readonly T[] | null, incoming: readonly T[], id: (row: T) => string,
  order: (a: T, b: T) => number, removed: ReadonlySet<string> = new Set(),
): T[] {
  const rows = new Map(incoming.map(row => [id(row), row]));
  for (const row of current ?? []) rows.set(id(row), row);
  return [...rows.values()].filter(row => !removed.has(id(row))).sort(order);
}

/** An aggregate-only event cannot establish the viewer's previous ballot. */
export function mergePollPage(
  current: readonly PollOut[] | null, incoming: readonly PollOut[],
  ownVoteKnown: ReadonlySet<string>, removed: ReadonlySet<string>,
): PollOut[] {
  const personal = new Map(incoming.map(row => [row.id, row.my_option_id]));
  return mergePageRows(current, incoming, row => row.id, pollOrder, removed).map(row =>
    !ownVoteKnown.has(row.id) && personal.has(row.id)
      ? { ...row, my_option_id: personal.get(row.id)! } : row,
  );
}

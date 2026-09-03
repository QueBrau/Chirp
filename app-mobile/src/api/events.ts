/**
 * Events API (DESIGN §8.7 - "the Partiful corner"): org events, invites and RSVPs.
 * Mirrors backend routers/events.py (c33, c198).
 */

import { request } from "./client";

export type RsvpStatus = "going" | "maybe" | "cant";

/**
 * How far an event travels, narrowest first. Mirrors the events.visibility CHECK.
 *
 *   chapter  - active members of the hosting chapter only
 *   campus   - .edu-verified students of this chapter's campus
 *   verified - any .edu-verified user (sister chapter, another school)
 *   public   - anyone at all, no account needed
 *
 * An INVITE is a separate axis and admits one named person whatever this says.
 */
export type EventVisibility = "chapter" | "campus" | "verified" | "public";

export interface EventCreate {
  title: string;
  /** ISO-8601 instant. Replaced the old free-text date_label in c198. */
  starts_at: string;
  location: string;
  cover_url: string;
  description?: string | null;
  ends_at?: string | null;
  /** Omitted means 'chapter' - the server defaults to the narrowest tier, never wider. */
  visibility?: EventVisibility;
}

/**
 * A host's edit. Omitted fields are left alone (PATCH semantics); an explicit `null`
 * on ends_at or description CLEARS it (c202) - the backend's exclude_unset check
 * (routers/events.py update_event) distinguishes "key not sent" from "key sent as
 * null", so those two fields carry `| null` here where the rest do not.
 */
export interface EventUpdate {
  title?: string;
  starts_at?: string;
  ends_at?: string | null;
  location?: string;
  cover_url?: string;
  description?: string | null;
  visibility?: EventVisibility;
}

export interface EventOut {
  id: string;
  chapter_id: string;
  title: string;
  /** Picsum-seeded cover photo (§10.2 - real imagery over placeholders). */
  cover_url: string;
  description: string | null;
  starts_at: string;
  ends_at: string | null;
  location: string;
  visibility: EventVisibility;
  /** Non-null means the party is off. The row survives so guests can be told. */
  canceled_at: string | null;
  host_id: string;
  created_at: string;
}

export interface EventInviteOut {
  event_id: string;
  invited_user_id: string;
  invited_by: string;
  created_at: string;
}

export interface EventRsvpOut {
  event_id: string;
  user_id: string;
  status: RsvpStatus;
  created_at: string;
}

/**
 * Cursor options for the split guest-list routes (c275). The tie-break key is a
 * USER id, not a row id - rsvp/invite rows have composite primary keys - so the
 * second cursor param carries the last row's user_id / invited_user_id.
 */
export interface GuestListPage {
  after?: string;
  afterUserId?: string;
  limit?: number;
}

/** All events for a chapter (Events segment, §8.7), soonest-first by start time. */
export async function listEvents(chapterId: string): Promise<EventOut[]> {
  return request<EventOut[]>(`/chapters/${chapterId}/events`);
}

export async function createEvent(chapterId: string, body: EventCreate): Promise<EventOut> {
  return request<EventOut>(`/chapters/${chapterId}/events`, { method: "POST", body });
}

export async function getEvent(eventId: string): Promise<EventOut> {
  return request<EventOut>(`/events/${eventId}`);
}

/** Edit an event. Host or e-board only; a cancelled event is not editable. */
export async function updateEvent(eventId: string, body: EventUpdate): Promise<EventOut> {
  return request<EventOut>(`/events/${eventId}`, { method: "PATCH", body });
}

/** Call the party off. Idempotent - the first cancellation moment is the one kept. */
export async function cancelEvent(eventId: string): Promise<EventOut> {
  return request<EventOut>(`/events/${eventId}/cancel`, { method: "POST" });
}

/**
 * Invite people. Host or e-board only, because an invite GRANTS READ ACCESS.
 * Returns the event's full invite list, so the caller need not track which were new.
 */
export async function inviteToEvent(
  eventId: string,
  userIds: string[],
): Promise<EventInviteOut[]> {
  return request<EventInviteOut[]>(`/events/${eventId}/invites`, {
    method: "POST",
    body: { user_ids: userIds },
  });
}

// listGuests / EventGuestsOut are GONE (c275): the wrapper returned both guest
// lists unbounded. Its halves are listRsvps + listEventInvites below; headcounts
// come from getRsvpCounts, never from summing pages.

/** One page of an event's invites, earliest first. Guest-list gated. */
export async function listEventInvites(
  eventId: string,
  page: GuestListPage = {},
): Promise<EventInviteOut[]> {
  return request<EventInviteOut[]>(`/events/${eventId}/invites`, {
    query: { after: page.after, after_user_id: page.afterUserId, limit: page.limit },
  });
}

/** Headcounts by answer plus silent invitees - the planning number (c275). */
export async function getRsvpCounts(eventId: string): Promise<EventRsvpCountsOut> {
  return request<EventRsvpCountsOut>(`/events/${eventId}/rsvp-counts`);
}

/** Events the signed-in user was invited to. Cancelled ones are included on purpose. */
export async function listMyInvites(): Promise<EventOut[]> {
  return request<EventOut[]>("/me/event-invites");
}

/**
 * One row of listMyInvitesWithRsvps() - mirrors backend EventInviteWithRsvpOut (c204).
 *
 * my_rsvp_status is the CALLER'S OWN rsvp (null if they haven't answered) - never
 * another invitee's. hosted_by is the hosting chapter's display name, resolved
 * server-side and safe to show even though the caller may not be a member of that
 * chapter - an invite already admits them to the event, which shows who hosts it.
 */
export interface EventInviteWithRsvpOut {
  event: EventOut;
  my_rsvp_status: RsvpStatus | null;
  hosted_by: string;
}

/**
 * Bulk sibling of listMyInvites() (c204): the same invited events, plus each one's
 * own-rsvp status and chapter label in a single round trip. Replaces the Home screen's
 * per-invite listGuests() + getChapter() N+1 (see feed/index.tsx's old
 * loadVisibleInvites for the shape this collapses).
 */
export async function listMyInvitesWithRsvps(): Promise<EventInviteWithRsvpOut[]> {
  return request<EventInviteWithRsvpOut[]>("/me/event-invites-with-rsvps");
}

/** One page of an event's RSVPs, earliest answers first. Guest-list gated. */
export async function listRsvps(
  eventId: string,
  page: GuestListPage = {},
): Promise<EventRsvpOut[]> {
  return request<EventRsvpOut[]>(`/events/${eventId}/rsvps`, {
    query: { after: page.after, after_user_id: page.afterUserId, limit: page.limit },
  });
}

/** Headcounts for one event - mirrors backend EventRsvpCountsOut (c275). */
export interface EventRsvpCountsOut {
  going: number;
  maybe: number;
  cant: number;
  invited_unanswered: number;
}

/** One row of listEventsWithRsvps() - mirrors backend EventWithRsvpSummaryOut (c280).
 * counts is the truth; going_preview is the first few going answers, display-sized
 * for the avatar stack and never claimed complete. */
export interface EventWithRsvpSummaryOut {
  event: EventOut;
  counts: EventRsvpCountsOut;
  going_preview: EventRsvpOut[];
  my_rsvp_status: RsvpStatus | null;
}

/** The chapter's events with their RSVP summaries in ONE round trip (c43 shape,
 * re-cut by c280) - replaces the Events segment's listEvents + listRsvps-per-event
 * 1+N, without shipping a campus of rows per popular event. */
export async function listEventsWithRsvps(
  chapterId: string,
): Promise<EventWithRsvpSummaryOut[]> {
  return request<EventWithRsvpSummaryOut[]>(`/chapters/${chapterId}/events-with-rsvps`);
}

/** Upserts the current user's RSVP for an event (Going / Maybe / Can't, §8.7). */
export async function setRsvp(eventId: string, status: RsvpStatus): Promise<EventRsvpOut> {
  return request<EventRsvpOut>(`/events/${eventId}/rsvps`, { method: "PUT", body: { status } });
}

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
 * The guest list. Invites and RSVPs come back SEPARATELY rather than merged, because
 * the screen has to tell "invited, has not answered" from "said no" - and because a
 * campus or public event routinely produces RSVPs from people nobody invited.
 */
export interface EventGuestsOut {
  invites: EventInviteOut[];
  rsvps: EventRsvpOut[];
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

/** Who was invited and how everyone answered. Never public - see routers/events.py. */
export async function listGuests(eventId: string): Promise<EventGuestsOut> {
  return request<EventGuestsOut>(`/events/${eventId}/guests`);
}

/** Events the signed-in user was invited to. Cancelled ones are included on purpose. */
export async function listMyInvites(): Promise<EventOut[]> {
  return request<EventOut[]>("/me/event-invites");
}

export async function listRsvps(eventId: string): Promise<EventRsvpOut[]> {
  return request<EventRsvpOut[]>(`/events/${eventId}/rsvps`);
}

/** One row of listEventsWithRsvps() - mirrors backend EventWithRsvpsOut (c43). */
export interface EventWithRsvpsOut {
  event: EventOut;
  rsvps: EventRsvpOut[];
}

/** The chapter's events with all their RSVPs in ONE round trip (c43) - replaces the
 * Events segment's listEvents + listRsvps-per-event 1+N. */
export async function listEventsWithRsvps(chapterId: string): Promise<EventWithRsvpsOut[]> {
  return request<EventWithRsvpsOut[]>(`/chapters/${chapterId}/events-with-rsvps`);
}

/** Upserts the current user's RSVP for an event (Going / Maybe / Can't, §8.7). */
export async function setRsvp(eventId: string, status: RsvpStatus): Promise<EventRsvpOut> {
  return request<EventRsvpOut>(`/events/${eventId}/rsvps`, { method: "PUT", body: { status } });
}

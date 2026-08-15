/**
 * Events API (DESIGN §8.7 — "the Partiful corner"): org events + RSVPs, scoped
 * to a chapter.
 */

import { request } from "./client";

export type RsvpStatus = "going" | "maybe" | "cant";

export interface EventCreate {
  title: string;
  /** Human-readable date/time (mock — no calendar picker yet), e.g. "Sat, Sep 27 · 7:00 PM". */
  date_label: string;
  location: string;
  cover_url: string;
}

export interface EventOut {
  id: string;
  chapter_id: string;
  title: string;
  /** Picsum-seeded cover photo (§10.2 — real imagery over placeholders). */
  cover_url: string;
  /** Human-readable date/time — see EventCreate.date_label. */
  date_label: string;
  location: string;
  host_id: string;
  created_at: string;
}

export interface EventRsvpOut {
  event_id: string;
  user_id: string;
  status: RsvpStatus;
  created_at: string;
}

/** All events for a chapter (Events segment, §8.7). */
export async function listEvents(chapterId: string): Promise<EventOut[]> {
  return request<EventOut[]>(`/chapters/${chapterId}/events`);
}

export async function createEvent(chapterId: string, body: EventCreate): Promise<EventOut> {
  return request<EventOut>(`/chapters/${chapterId}/events`, { method: "POST", body });
}

export async function listRsvps(eventId: string): Promise<EventRsvpOut[]> {
  return request<EventRsvpOut[]>(`/events/${eventId}/rsvps`);
}

/** One row of listEventsWithRsvps() — mirrors backend EventWithRsvpsOut (c43). */
export interface EventWithRsvpsOut {
  event: EventOut;
  rsvps: EventRsvpOut[];
}

/** The chapter's events, newest first, each with all its RSVPs in ONE round trip
 * (c43) — replaces the Events segment's listEvents + listRsvps-per-event 1+N. */
export async function listEventsWithRsvps(chapterId: string): Promise<EventWithRsvpsOut[]> {
  return request<EventWithRsvpsOut[]>(`/chapters/${chapterId}/events-with-rsvps`);
}

/** Upserts the current user's RSVP for an event (Going / Maybe / Can't, §8.7). */
export async function setRsvp(eventId: string, status: RsvpStatus): Promise<EventRsvpOut> {
  return request<EventRsvpOut>(`/events/${eventId}/rsvps`, { method: "PUT", body: { status } });
}

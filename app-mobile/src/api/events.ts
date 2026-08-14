/**
 * Events API (DESIGN §8.7 — "the Partiful corner"): org events + RSVPs, scoped
 * to a chapter. Backed by mocks now; real events/event_rsvps tables are a
 * board card, not yet wired on the backend.
 */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_CURRENT_USER,
  MOCK_ORG_EVENT_RSVPS,
  MOCK_ORG_EVENTS,
  newMockId,
  nowIso,
} from "../mocks/data";

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
  if (USE_MOCKS) return mocked(MOCK_ORG_EVENTS.filter((e) => e.chapter_id === chapterId));
  return request<EventOut[]>(`/chapters/${chapterId}/events`);
}

export async function createEvent(chapterId: string, body: EventCreate): Promise<EventOut> {
  if (USE_MOCKS) {
    const event: EventOut = {
      id: newMockId("evt"),
      chapter_id: chapterId,
      title: body.title,
      cover_url: body.cover_url,
      date_label: body.date_label,
      location: body.location,
      host_id: MOCK_CURRENT_USER.id,
      created_at: nowIso(),
    };
    MOCK_ORG_EVENTS.push(event);
    return mocked(event);
  }
  return request<EventOut>(`/chapters/${chapterId}/events`, { method: "POST", body });
}

export async function listRsvps(eventId: string): Promise<EventRsvpOut[]> {
  if (USE_MOCKS) return mocked(MOCK_ORG_EVENT_RSVPS.filter((r) => r.event_id === eventId));
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
  if (USE_MOCKS) {
    return mocked(
      MOCK_ORG_EVENTS.filter((event) => event.chapter_id === chapterId).map((event) => ({
        event,
        rsvps: MOCK_ORG_EVENT_RSVPS.filter((rsvp) => rsvp.event_id === event.id),
      })),
    );
  }
  return request<EventWithRsvpsOut[]>(`/chapters/${chapterId}/events-with-rsvps`);
}

/** Upserts the current user's RSVP for an event (Going / Maybe / Can't, §8.7). */
export async function setRsvp(eventId: string, status: RsvpStatus): Promise<EventRsvpOut> {
  if (USE_MOCKS) {
    const existing = MOCK_ORG_EVENT_RSVPS.find(
      (r) => r.event_id === eventId && r.user_id === MOCK_CURRENT_USER.id,
    );
    if (existing) {
      existing.status = status;
      return mocked(existing);
    }
    const rsvp: EventRsvpOut = {
      event_id: eventId,
      user_id: MOCK_CURRENT_USER.id,
      status,
      created_at: nowIso(),
    };
    MOCK_ORG_EVENT_RSVPS.push(rsvp);
    return mocked(rsvp);
  }
  return request<EventRsvpOut>(`/events/${eventId}/rsvps`, { method: "PUT", body: { status } });
}

# Event detail and Home invitations (c351/c352)

Event detail reads the signed-in user's answer from
`GET /events/{id}/rsvps/mine`. The lookup uses the event/user key and the event read
permission, so it works before RSVP, for explicitly invited unverified users,
and when the user's answer is beyond the first guest page. It returns only that
user's status and does not grant access to anyone else's guest information.

Replies and invitations retain their bounded, ascending
`(created_at, user_id)` / `(created_at, invited_user_id)` pagination. The new
`unanswered_only=true` invitation filter applies the event-scoped anti-join before
pagination. Event detail never subtracts a partial RSVP page from a partial invite
page. It loads more replies and unanswered invitations on explicit actions and
uses `/rsvp-counts` for totals. If totals fail, they remain unavailable rather than
falling back to page lengths. The current answer also has an unknown/error state.

Refresh starts the guest cursors again and reads the current answer and totals.
Replies arriving from another device between page requests can change the current
membership of a group; refresh reconciles that state. Paging is not a database
snapshot. The invitation picker loads existing invitations through bounded pages
when opened, so its disabled members are not limited to the first page. Ordinary
event-detail loading does not automatically drain the roster.

Home requests `/me/event-invites-with-rsvps?view=actionable`. Selection happens
before the existing ascending `(starts_at, id)` cursor and limit:

- Unanswered upcoming events are included.
- Ongoing events with a future stated end are included.
- Upcoming cancellations remain visible even if the user already answered.
- Events whose end has passed, or whose start has passed without a stated end,
  are historical and no longer occupy Home's action list.

The API's default `view=all` preserves the existing history-inclusive contract;
`view=history` exposes past events. This change does not add a history screen.
Explicit invitations still grant visibility before school verification, and the
Home section stays above the campus-feed verification gate. Home refreshes on
focus and pull-to-refresh and exposes further pages with a Load more action.

The client helpers preserve both cursor fields. Because the existing list API
returns arrays, a full page exposes a continuation that can lead to one final
empty request. Page failures preserve the existing rows and cursor for retry.
Event start-time edits can move invitations relative to a previously captured
cursor; focus/refresh starts again, and appended pages deduplicate event IDs.

Backend regression fixtures cover 500 invitees, the caller's RSVP after row 200,
exact set equality across tied cursors, unanswered grouping, refresh, permission
checks, and a history overflow with 60 old answered invitations before pending
invitations. The existing event CI verifier also executes the real TypeScript API
and screen code using deterministic hooks and a simulated server: it checks own
selection, 500 rendered guest entries, continuation, refreshed cursors, unavailable
totals, and Home's actionable paging. That harness checks state and element props;
native rendering, accessibility, and device interaction remain device checks.

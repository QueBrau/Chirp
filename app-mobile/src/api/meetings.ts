/** Meetings API: minutes CRUD + attendance sheet — routers/meetings.py. */

import { request, requestText } from "./client";

export type AttendanceStatus = "present" | "absent" | "excused";

export interface MeetingCreate {
  title: string;
  meeting_date: string;
  minutes_md?: string | null;
}

export interface MeetingUpdate {
  title?: string | null;
  meeting_date?: string | null;
  minutes_md?: string | null;
}

export interface MeetingOut {
  id: string;
  chapter_id: string;
  title: string;
  meeting_date: string;
  minutes_md: string | null;
  created_by: string;
  created_at: string;
}

export interface MeetingAttendanceItem {
  user_id: string;
  status: AttendanceStatus;
}

/** Full attendance sheet for one meeting — PUT replaces existing records. */
export interface MeetingAttendanceUpdate {
  entries: MeetingAttendanceItem[];
}

export interface MeetingAttendanceOut {
  meeting_id: string;
  user_id: string;
  status: AttendanceStatus;
}

export async function listMeetings(chapterId: string): Promise<MeetingOut[]> {
  return request<MeetingOut[]>(`/chapters/${chapterId}/meetings`);
}

/**
 * Export meeting minutes as CSV — GET /chapters/{id}/meetings/export.csv. Returns
 * the raw CSV text; hand it to src/lib/export.ts's shareCsv() to write + open the
 * native share sheet.
 */
export async function exportMeetingsCsv(chapterId: string): Promise<string> {
  return requestText(`/chapters/${chapterId}/meetings/export.csv`);
}

export async function createMeeting(chapterId: string, body: MeetingCreate): Promise<MeetingOut> {
  return request<MeetingOut>(`/chapters/${chapterId}/meetings`, { method: "POST", body });
}

export async function updateMeeting(
  chapterId: string,
  meetingId: string,
  body: MeetingUpdate,
): Promise<MeetingOut> {
  return request<MeetingOut>(`/chapters/${chapterId}/meetings/${meetingId}`, {
    method: "PATCH",
    body,
  });
}

export async function getAttendance(
  chapterId: string,
  meetingId: string,
): Promise<MeetingAttendanceOut[]> {
  return request<MeetingAttendanceOut[]>(
    `/chapters/${chapterId}/meetings/${meetingId}/attendance`,
  );
}

/** Replace the full attendance sheet for a meeting. */
export async function putAttendance(
  chapterId: string,
  meetingId: string,
  body: MeetingAttendanceUpdate,
): Promise<MeetingAttendanceOut[]> {
  return request<MeetingAttendanceOut[]>(
    `/chapters/${chapterId}/meetings/${meetingId}/attendance`,
    { method: "PUT", body },
  );
}

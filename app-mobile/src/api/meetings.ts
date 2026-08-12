/** Meetings API: minutes CRUD + attendance sheet — routers/meetings.py. */

import { mocked, request, USE_MOCKS } from "./client";
import {
  MOCK_ATTENDANCE,
  MOCK_CURRENT_USER,
  MOCK_MEETINGS,
  newMockId,
  nowIso,
} from "../mocks/data";

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
  if (USE_MOCKS) return mocked(MOCK_MEETINGS.filter((m) => m.chapter_id === chapterId));
  return request<MeetingOut[]>(`/chapters/${chapterId}/meetings`);
}

export async function createMeeting(chapterId: string, body: MeetingCreate): Promise<MeetingOut> {
  if (USE_MOCKS) {
    const meeting: MeetingOut = {
      id: newMockId("mtg"),
      chapter_id: chapterId,
      title: body.title,
      meeting_date: body.meeting_date,
      minutes_md: body.minutes_md ?? null,
      created_by: MOCK_CURRENT_USER.id,
      created_at: nowIso(),
    };
    MOCK_MEETINGS.push(meeting);
    return mocked(meeting);
  }
  return request<MeetingOut>(`/chapters/${chapterId}/meetings`, { method: "POST", body });
}

export async function updateMeeting(
  chapterId: string,
  meetingId: string,
  body: MeetingUpdate,
): Promise<MeetingOut> {
  if (USE_MOCKS) {
    const meeting = MOCK_MEETINGS.find((m) => m.id === meetingId);
    if (meeting) {
      if (body.title != null) meeting.title = body.title;
      if (body.meeting_date != null) meeting.meeting_date = body.meeting_date;
      if (body.minutes_md !== undefined) meeting.minutes_md = body.minutes_md;
      return mocked(meeting);
    }
    return mocked(MOCK_MEETINGS[0]);
  }
  return request<MeetingOut>(`/chapters/${chapterId}/meetings/${meetingId}`, {
    method: "PATCH",
    body,
  });
}

export async function getAttendance(
  chapterId: string,
  meetingId: string,
): Promise<MeetingAttendanceOut[]> {
  if (USE_MOCKS) return mocked(MOCK_ATTENDANCE.filter((a) => a.meeting_id === meetingId));
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
  if (USE_MOCKS) {
    for (let i = MOCK_ATTENDANCE.length - 1; i >= 0; i -= 1) {
      if (MOCK_ATTENDANCE[i].meeting_id === meetingId) MOCK_ATTENDANCE.splice(i, 1);
    }
    const rows: MeetingAttendanceOut[] = body.entries.map((entry) => ({
      meeting_id: meetingId,
      user_id: entry.user_id,
      status: entry.status,
    }));
    MOCK_ATTENDANCE.push(...rows);
    return mocked(rows);
  }
  return request<MeetingAttendanceOut[]>(
    `/chapters/${chapterId}/meetings/${meetingId}/attendance`,
    { method: "PUT", body },
  );
}

/** Secretary: meetings with minutes preview + attendance tallies from mocks. */

import { useEffect, useState } from "react";
import { View } from "react-native";

import {
  getAttendance,
  listMeetings,
  type MeetingAttendanceOut,
  type MeetingOut,
} from "@/api/meetings";
import { AppText, Badge, Card, EmptyState, Screen } from "@/components";
import { MOCK_CURRENT_MEMBERSHIP } from "@/mocks/data";
import { spacing } from "@/theme";

interface MeetingItem {
  meeting: MeetingOut;
  attendance: MeetingAttendanceOut[];
}

function meetingDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export default function SecretaryScreen() {
  const [items, setItems] = useState<MeetingItem[] | null>(null);

  useEffect(() => {
    const load = async () => {
      const chapterId = MOCK_CURRENT_MEMBERSHIP.chapter_id;
      const meetings = await listMeetings(chapterId);
      const withAttendance = await Promise.all(
        meetings.map(async (meeting) => ({
          meeting,
          attendance: await getAttendance(chapterId, meeting.id),
        })),
      );
      // Most recent first.
      withAttendance.sort((a, b) => b.meeting.meeting_date.localeCompare(a.meeting.meeting_date));
      setItems(withAttendance);
    };
    void load();
  }, []);

  return (
    <Screen title="Secretary" subtitle="Minutes and attendance">
      {items !== null && items.length === 0 ? (
        <EmptyState title="No meetings yet" message="Create a meeting to start taking minutes." />
      ) : (
        <View style={{ gap: spacing.md }}>
          {(items ?? []).map(({ meeting, attendance }) => {
            const count = (status: MeetingAttendanceOut["status"]) =>
              attendance.filter((a) => a.status === status).length;
            return (
              <Card key={meeting.id}>
                <View style={{ gap: spacing.sm }}>
                  <AppText variant="title">{meeting.title}</AppText>
                  <AppText variant="caption" tone="secondary">
                    {meetingDate(meeting.meeting_date)}
                  </AppText>
                  {meeting.minutes_md !== null ? (
                    <AppText variant="caption" tone="secondary" numberOfLines={4}>
                      {meeting.minutes_md}
                    </AppText>
                  ) : (
                    <AppText variant="caption" tone="tertiary">
                      No minutes recorded yet.
                    </AppText>
                  )}
                  <View style={{ flexDirection: "row", gap: spacing.sm, flexWrap: "wrap" }}>
                    <Badge label={`${count("present")} present`} tone="success" />
                    <Badge label={`${count("absent")} absent`} tone="danger" />
                    <Badge label={`${count("excused")} excused`} tone="neutral" />
                  </View>
                </View>
              </Card>
            );
          })}
        </View>
      )}
    </Screen>
  );
}

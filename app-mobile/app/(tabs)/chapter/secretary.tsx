/**
 * Secretary per DESIGN §7: meetings as cards — title, date caption, minutes
 * preview, attendance Chips. Adds the secretary's write surface: create a
 * meeting, edit its minutes, take attendance against the chapter roster, and
 * export all minutes as CSV.
 *
 * Role-gated (SPEC §8.4/§8.2): only the chapter's secretary or president may
 * hit the meetings/attendance endpoints, which the backend enforces via
 * `require_role`. The real chapter_id + role come from `GET /me/memberships`
 * (myMemberships()) rather than the mock membership, so a non-eligible user
 * sees an EmptyState instead of a wall of 403s.
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Pressable, TextInput, View } from "react-native";

import { listMembers, myMemberships, type MembershipOut, type MyMembershipOut } from "@/api/chapters";
import { ApiError } from "@/api/client";
import {
  createMeeting,
  exportMeetingsCsv,
  getAttendance,
  listMeetings,
  putAttendance,
  updateMeeting,
  type AttendanceStatus,
  type MeetingAttendanceOut,
  type MeetingOut,
} from "@/api/meetings";
import {
  AppText,
  Button,
  Card,
  Chip,
  EmptyState,
  GradientAvatar,
  ListRow,
  Screen,
  SectionHeader,
} from "@/components";
import { shareCsv } from "@/lib/export";
import { radii, spacing, typography, useTheme, type Palette } from "@/theme";

interface MeetingItem {
  meeting: MeetingOut;
  attendance: MeetingAttendanceOut[];
}

/** Which per-meeting panel (if any) is expanded — only one at a time, across all cards. */
type ExpandedPanel = { meetingId: string; kind: "minutes" | "attendance" } | null;

const ATTENDANCE_OPTIONS: { key: AttendanceStatus; label: string; short: string }[] = [
  { key: "present", label: "present", short: "P" },
  { key: "absent", label: "absent", short: "A" },
  { key: "excused", label: "excused", short: "E" },
];

function meetingDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

/** ApiError carries a server-provided `.detail`; anything else gets a generic fallback. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  Alert.alert(title, message);
}

/**
 * Strict `YYYY-MM-DD` -> ISO date-time string (midnight UTC). Rejects anything
 * that doesn't match the format or isn't a real calendar date instead of
 * silently coercing it into `Invalid Date` and POSTing that — e.g. `2026-02-30`
 * would otherwise roll over to March 2 via the Date constructor's normal
 * "overflow" behavior. No date-picker dependency is available, so this is a
 * plain text field with an explicit expected format.
 */
function parseMeetingDate(raw: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw.trim());
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null;
  }
  return parsed.toISOString();
}

function statusTagColors(palette: Palette, status: AttendanceStatus): { bg: string; fg: string } {
  switch (status) {
    case "present":
      return { bg: palette.successSoft, fg: palette.success };
    case "absent":
      return { bg: palette.dangerSoft, fg: palette.danger };
    case "excused":
      return { bg: palette.warningSoft, fg: palette.warning };
  }
}

function FieldLabel({ children }: { children: string }) {
  return (
    <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
      {children}
    </AppText>
  );
}

export default function SecretaryScreen() {
  const palette = useTheme();

  // undefined = /me/memberships hasn't resolved yet; null = signed-in user has
  // no secretary/president membership anywhere (role-gated screen — NO further
  // calls, the meetings/attendance endpoints would just 403); object = the
  // real membership driving every call below.
  const [membership, setMembership] = useState<MyMembershipOut | null | undefined>(undefined);
  const [items, setItems] = useState<MeetingItem[] | null>(null);
  const [roster, setRoster] = useState<MembershipOut[] | null>(null);

  // New-meeting form state.
  const [newTitle, setNewTitle] = useState("");
  const [newDateText, setNewDateText] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [creatingMeeting, setCreatingMeeting] = useState(false);

  // Per-meeting minutes/attendance editors.
  const [expanded, setExpanded] = useState<ExpandedPanel>(null);
  const [minutesDraft, setMinutesDraft] = useState("");
  const [savingMinutes, setSavingMinutes] = useState(false);
  const [attendanceDraft, setAttendanceDraft] = useState<Record<string, AttendanceStatus>>({});
  const [savingAttendance, setSavingAttendance] = useState(false);

  const [exportingCsv, setExportingCsv] = useState(false);

  const chapterId = membership?.chapter_id ?? null;

  const loadDashboard = useCallback(async (id: string) => {
    const [meetings, members] = await Promise.all([listMeetings(id), listMembers(id)]);
    const withAttendance = await Promise.all(
      meetings.map(async (meeting) => ({
        meeting,
        attendance: await getAttendance(id, meeting.id),
      })),
    );
    // Most recent first.
    withAttendance.sort((a, b) => b.meeting.meeting_date.localeCompare(a.meeting.meeting_date));
    setRoster(members.filter((m) => m.status === "active"));
    setItems(withAttendance);
  }, []);

  useEffect(() => {
    const init = async () => {
      try {
        const memberships = await myMemberships();
        const eligible =
          memberships.find((m) => m.role === "secretary" || m.role === "president") ?? null;
        setMembership(eligible);
        if (eligible === null) return; // role-gated: no meetings/attendance calls
        await loadDashboard(eligible.chapter_id);
      } catch (error) {
        showApiError(error, "Couldn't load the secretary dashboard");
        setMembership(null);
      }
    };
    void init();
  }, [loadDashboard]);

  if (membership === null) {
    return (
      <Screen title="Secretary" subtitle="Minutes and attendance">
        <EmptyState
          title="Secretary/president only"
          message="This dashboard is limited to the chapter's secretary or president."
        />
      </Screen>
    );
  }

  const activeRoster = roster ?? [];

  const inputStyle = {
    ...typography.body,
    color: palette.ink,
    backgroundColor: palette.surfaceAlt,
    borderRadius: radii.input,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  };

  const handleCreateMeeting = async () => {
    if (chapterId === null || creatingMeeting) return; // double-submit guard
    const title = newTitle.trim();
    if (title.length === 0) {
      setCreateError("Give the meeting a title.");
      return;
    }
    const isoDate = parseMeetingDate(newDateText);
    if (isoDate === null) {
      setCreateError("Enter the date as YYYY-MM-DD, e.g. 2026-09-14.");
      return;
    }
    setCreateError(null);
    setCreatingMeeting(true);
    try {
      const created = await createMeeting(chapterId, { title, meeting_date: isoDate });
      setItems((current) => {
        const next = [
          { meeting: created, attendance: [] as MeetingAttendanceOut[] },
          ...(current ?? []),
        ];
        // Re-sort rather than assume the new meeting is newest — a secretary
        // logging a past meeting shouldn't jump the most-recent-first order.
        next.sort((a, b) => b.meeting.meeting_date.localeCompare(a.meeting.meeting_date));
        return next;
      });
      setNewTitle("");
      setNewDateText("");
    } catch (error) {
      showApiError(error, "Couldn't create meeting");
    } finally {
      setCreatingMeeting(false);
    }
  };

  const openMinutesEditor = (meeting: MeetingOut) => {
    // Load the current value in before editing — updateMeeting PATCHes the
    // whole minutes_md field, so saving a draft that never saw the existing
    // text would blank it out.
    setMinutesDraft(meeting.minutes_md ?? "");
    setExpanded({ meetingId: meeting.id, kind: "minutes" });
  };

  const openAttendanceEditor = (meeting: MeetingOut, attendance: MeetingAttendanceOut[]) => {
    const draft: Record<string, AttendanceStatus> = {};
    for (const member of activeRoster) {
      draft[member.user_id] = attendance.find((a) => a.user_id === member.user_id)?.status ?? "absent";
    }
    setAttendanceDraft(draft);
    setExpanded({ meetingId: meeting.id, kind: "attendance" });
  };

  const closeExpanded = () => setExpanded(null);

  const saveMinutes = async (meeting: MeetingOut) => {
    if (chapterId === null || savingMinutes) return; // double-submit guard
    setSavingMinutes(true);
    try {
      const trimmed = minutesDraft.trim();
      const updated = await updateMeeting(chapterId, meeting.id, {
        minutes_md: trimmed.length > 0 ? trimmed : null,
      });
      setItems((current) =>
        (current ?? []).map((it) => (it.meeting.id === meeting.id ? { ...it, meeting: updated } : it)),
      );
      setExpanded(null);
    } catch (error) {
      showApiError(error, "Couldn't save minutes");
    } finally {
      setSavingMinutes(false);
    }
  };

  const saveAttendance = async (meeting: MeetingOut) => {
    if (chapterId === null || savingAttendance) return; // double-submit guard
    setSavingAttendance(true);
    try {
      const entries = Object.entries(attendanceDraft).map(([user_id, status]) => ({ user_id, status }));
      // putAttendance is a full-sheet bulk upsert — safe to re-save.
      const saved = await putAttendance(chapterId, meeting.id, { entries });
      setItems((current) =>
        (current ?? []).map((it) => (it.meeting.id === meeting.id ? { ...it, attendance: saved } : it)),
      );
      setExpanded(null);
    } catch (error) {
      showApiError(error, "Couldn't save attendance");
    } finally {
      setSavingAttendance(false);
    }
  };

  const exportCsv = async () => {
    if (chapterId === null || exportingCsv) return;
    setExportingCsv(true);
    try {
      const csv = await exportMeetingsCsv(chapterId);
      const today = new Date().toISOString().slice(0, 10);
      const filename = `${membership?.chapter_name ?? "chapter"} meetings ${today}`;
      try {
        await shareCsv(filename, csv);
      } catch {
        // expo-file-system/expo-sharing are newly-added native modules — until the
        // EAS dev build is rebuilt, shareCsv fails at native-module resolution even
        // though the CSV text above resolved fine. Surface that plainly instead of
        // letting it fall through as an unhandled rejection.
        Alert.alert(
          "Can't share yet",
          "The CSV was generated, but sharing needs a native module that isn't in this " +
            "build yet. Rebuild the app (EAS dev build) and try again.",
        );
      }
    } catch (error) {
      showApiError(error, "Couldn't export meeting minutes");
    } finally {
      setExportingCsv(false);
    }
  };

  return (
    <Screen title="Secretary" subtitle="Minutes and attendance">
      <View style={{ gap: spacing.xl }}>
        <View>
          <SectionHeader title="New meeting" caption="Title and date — take minutes and attendance after" />
          <Card>
            <View style={{ gap: spacing.lg }}>
              <View>
                <FieldLabel>Title</FieldLabel>
                <TextInput
                  value={newTitle}
                  onChangeText={setNewTitle}
                  placeholder="e.g. Weekly Chapter Meeting"
                  placeholderTextColor={palette.inkFaint}
                  style={inputStyle}
                />
              </View>
              <View>
                <FieldLabel>Meeting date (YYYY-MM-DD)</FieldLabel>
                <TextInput
                  value={newDateText}
                  onChangeText={setNewDateText}
                  placeholder="2026-09-14"
                  placeholderTextColor={palette.inkFaint}
                  style={inputStyle}
                />
              </View>
              {createError !== null ? (
                <AppText variant="caption" tone="danger">
                  {createError}
                </AppText>
              ) : null}
              <Button
                label={creatingMeeting ? "Creating…" : "Create meeting"}
                onPress={() => void handleCreateMeeting()}
                disabled={creatingMeeting}
              />
            </View>
          </Card>
        </View>

        <View>
          <SectionHeader
            title="Meetings"
            caption="Most recent first"
            right={
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Export meetings as CSV"
                accessibilityState={{ disabled: exportingCsv }}
                disabled={exportingCsv}
                onPress={() => void exportCsv()}
                hitSlop={spacing.sm}
              >
                <AppText variant="bodyBold" tone={exportingCsv ? "tertiary" : "accent"}>
                  {exportingCsv ? "Exporting…" : "Export CSV"}
                </AppText>
              </Pressable>
            }
          />
          {items !== null && items.length === 0 ? (
            <EmptyState
              title="No meetings yet"
              message="Create a meeting to start taking minutes."
            />
          ) : (
            <View style={{ gap: spacing.md }}>
              {(items ?? []).map(({ meeting, attendance }) => {
                const count = (status: AttendanceStatus) =>
                  attendance.filter((a) => a.status === status).length;
                const minutesOpen = expanded?.meetingId === meeting.id && expanded.kind === "minutes";
                const attendanceOpen =
                  expanded?.meetingId === meeting.id && expanded.kind === "attendance";

                return (
                  <Card key={meeting.id}>
                    <View style={{ gap: spacing.sm }}>
                      <AppText variant="title">{meeting.title}</AppText>
                      <AppText variant="caption" tone="tertiary">
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
                      <View
                        style={{
                          flexDirection: "row",
                          gap: spacing.sm,
                          flexWrap: "wrap",
                          marginTop: spacing.xs,
                        }}
                      >
                        <Chip label={`${count("present")} present`} variant="success" />
                        <Chip label={`${count("absent")} absent`} variant="danger" />
                        <Chip label={`${count("excused")} excused`} variant="neutral" />
                      </View>

                      <View style={{ flexDirection: "row", gap: spacing.lg, marginTop: spacing.xs }}>
                        <Pressable
                          accessibilityRole="button"
                          accessibilityLabel={minutesOpen ? "Close minutes editor" : "Edit minutes"}
                          onPress={() =>
                            minutesOpen ? closeExpanded() : openMinutesEditor(meeting)
                          }
                          hitSlop={spacing.sm}
                        >
                          <AppText variant="bodyBold" tone="accent">
                            {minutesOpen ? "Close" : "Edit minutes"}
                          </AppText>
                        </Pressable>
                        <Pressable
                          accessibilityRole="button"
                          accessibilityLabel={
                            attendanceOpen ? "Close attendance editor" : "Take attendance"
                          }
                          onPress={() =>
                            attendanceOpen
                              ? closeExpanded()
                              : openAttendanceEditor(meeting, attendance)
                          }
                          hitSlop={spacing.sm}
                        >
                          <AppText variant="bodyBold" tone="accent">
                            {attendanceOpen ? "Close" : "Take attendance"}
                          </AppText>
                        </Pressable>
                      </View>

                      {minutesOpen ? (
                        <View style={{ gap: spacing.sm, marginTop: spacing.sm }}>
                          <TextInput
                            value={minutesDraft}
                            onChangeText={setMinutesDraft}
                            placeholder="Write the minutes in markdown…"
                            placeholderTextColor={palette.inkFaint}
                            multiline
                            style={[inputStyle, { minHeight: 140, textAlignVertical: "top" }]}
                          />
                          <Button
                            label={savingMinutes ? "Saving…" : "Save minutes"}
                            onPress={() => void saveMinutes(meeting)}
                            disabled={savingMinutes}
                          />
                        </View>
                      ) : null}

                      {attendanceOpen ? (
                        <View style={{ marginTop: spacing.sm }}>
                          {activeRoster.length === 0 ? (
                            <AppText variant="caption" tone="tertiary">
                              No active members on the roster yet.
                            </AppText>
                          ) : (
                            activeRoster.map((member, index) => {
                              // GET /chapters/{id}/members joins the name in; there is no
                              // GET /users/{id}, so this is the only real-data source for
                              // it. Fall back to the id rather than "Unknown" — taking
                              // attendance against an unidentifiable row is worse than ugly.
                              const name = member.display_name ?? member.user_id;
                              const status = attendanceDraft[member.user_id] ?? "absent";
                              return (
                                <ListRow
                                  key={member.user_id}
                                  title={name}
                                  left={
                                    // No photoUrl: MembershipOut carries display_name but not
                                    // avatar_url, so the roster falls back to initials.
                                    <GradientAvatar name={name} size={32} />
                                  }
                                  divider={index < activeRoster.length - 1}
                                  right={
                                    <View style={{ flexDirection: "row", gap: spacing.xs }}>
                                      {ATTENDANCE_OPTIONS.map((option) => {
                                        const selected = status === option.key;
                                        const colors = statusTagColors(palette, option.key);
                                        return (
                                          <Pressable
                                            key={option.key}
                                            accessibilityRole="button"
                                            accessibilityLabel={`Mark ${name} ${option.label}`}
                                            accessibilityState={{ selected }}
                                            onPress={() =>
                                              setAttendanceDraft((current) => ({
                                                ...current,
                                                [member.user_id]: option.key,
                                              }))
                                            }
                                            style={{
                                              paddingHorizontal: spacing.sm,
                                              paddingVertical: spacing.xs,
                                              borderRadius: radii.pill,
                                              backgroundColor: selected ? colors.bg : palette.surfaceAlt,
                                            }}
                                          >
                                            <AppText
                                              variant="micro"
                                              style={{ color: selected ? colors.fg : palette.inkSecondary }}
                                            >
                                              {option.short}
                                            </AppText>
                                          </Pressable>
                                        );
                                      })}
                                    </View>
                                  }
                                />
                              );
                            })
                          )}
                          <Button
                            label={savingAttendance ? "Saving…" : "Save attendance"}
                            onPress={() => void saveAttendance(meeting)}
                            disabled={savingAttendance}
                            style={{ marginTop: spacing.sm }}
                          />
                        </View>
                      ) : null}
                    </View>
                  </Card>
                );
              })}
            </View>
          )}
        </View>
      </View>
    </Screen>
  );
}

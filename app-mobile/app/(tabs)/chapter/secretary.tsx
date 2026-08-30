/**
 * Secretary per DESIGN §7: meetings as cards — title, date caption, minutes
 * preview, attendance Chips. Adds the secretary's write surface: create a
 * meeting, edit its minutes, take attendance against the chapter roster,
 * run live polls (c162), and export all minutes as CSV.
 *
 * Role-gated (SPEC §8.4/§8.2): only the chapter's secretary or president may
 * hit the meetings/attendance endpoints, which the backend enforces via
 * `require_role`. The real chapter_id + role come from `GET /me/memberships`
 * (myMemberships()) rather than the mock membership, so a non-eligible user
 * sees an EmptyState instead of a wall of 403s.
 */

import { useCallback, useEffect, useState } from "react";
import { Pressable, TextInput, View } from "react-native";

import { listMembers, myMemberships, type MemberOut, type MyMembershipOut } from "@/api/chapters";
import {
  createMeeting,
  deleteMeeting,
  exportMeetingsCsv,
  getAttendanceSummary,
  listMeetingsWithAttendance,
  putAttendance,
  updateMeeting,
  type AttendanceStatus,
  type ChapterAttendanceSummary,
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
  PollCard,
  Screen,
  SectionHeader,
} from "@/components";
import {
  castVote,
  closePoll,
  createPoll,
  listPolls,
  type PollOut,
} from "@/api/polls";
import { confirmAction, showAlert, showApiError } from "@/lib/alert";
import { calendarDay } from "@/lib/dates";
import { shareCsv } from "@/lib/export";
import { currentSemesterWindow } from "@/org/semester";
import { chirpSocket, isPollEvent } from "@/realtime/socket";
import { inputField, radii, spacing, useTheme, type Palette } from "@/theme";

interface MeetingItem {
  meeting: MeetingOut;
  attendance: MeetingAttendanceOut[];
}

/** Which per-meeting panel (if any) is expanded — only one at a time, across all cards. */
type ExpandedPanel = { meetingId: string; kind: "minutes" | "attendance" } | null;

/**
 * Fixed option slots on the new-poll form. Blanks are ignored, so a Yes/No poll
 * is two taps and nobody has to remove empty rows. Four covers Yes/No/Abstain
 * plus one; the server accepts up to ten, so a dynamic add-a-row form is a later
 * change here and needs nothing on the backend.
 */
const POLL_OPTION_SLOTS = 4;

const ATTENDANCE_OPTIONS: { key: AttendanceStatus; label: string; short: string }[] = [
  { key: "present", label: "present", short: "P" },
  { key: "absent", label: "absent", short: "A" },
  { key: "excused", label: "excused", short: "E" },
];

function meetingDate(iso: string): string {
  // c165: meeting_date is a calendar DAY, not an instant. Formatting the instant
  // directly rendered the previous day everywhere west of UTC.
  return calendarDay(iso).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
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

/** Which attendance window the roster report is showing. */
type WindowKey = "semester" | "all";

const WINDOW_OPTIONS: { key: WindowKey; label: string }[] = [
  { key: "semester", label: "Semester" },
  { key: "all", label: "All time" },
];

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
  const [roster, setRoster] = useState<MemberOut[] | null>(null);

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

  // Live polls (c162).
  const [polls, setPolls] = useState<PollOut[] | null>(null);
  const [newQuestion, setNewQuestion] = useState("");
  const [newOptions, setNewOptions] = useState<string[]>(
    Array.from({ length: POLL_OPTION_SLOTS }, () => ""),
  );
  const [pollError, setPollError] = useState<string | null>(null);
  const [creatingPoll, setCreatingPoll] = useState(false);
  // Which poll has a request in flight, so only that card disables.
  const [busyPollId, setBusyPollId] = useState<string | null>(null);

  // Roster attendance totals (board c82) — one server call for the whole chapter.
  const [summary, setSummary] = useState<ChapterAttendanceSummary | null>(null);
  const [windowKey, setWindowKey] = useState<WindowKey>("semester");
  const [deletingMeetingId, setDeletingMeetingId] = useState<string | null>(null);

  const chapterId = membership?.chapter_id ?? null;

  /**
   * Deliberately swallows its own errors instead of throwing to the caller: a failed
   * totals call must not take the minutes and attendance surfaces down with it, which
   * is what happens if this rejects inside init() below.
   */
  const loadSummary = useCallback(async (id: string, key: WindowKey) => {
    try {
      const totals = await getAttendanceSummary(
        id,
        key === "semester" ? currentSemesterWindow(new Date()) : {},
      );
      setSummary(totals);
    } catch (error) {
      showApiError(error, "Couldn't load attendance totals");
    }
  }, []);

  /**
   * Exactly two requests, whatever the chapter's history looks like (board c156).
   * This used to be listMeetings followed by getAttendance PER MEETING inside a
   * Promise.all — a semester of meetings meant a semester of requests every time the
   * dashboard opened, and it grew with the archive rather than with anything the
   * secretary asked for. The server returns most-recent-first, so there is no sort
   * here any more; the create path below still sorts, because a meeting logged for a
   * past date must not jump to the top.
   */
  const loadDashboard = useCallback(async (id: string) => {
    const [withAttendance, members, chapterPolls] = await Promise.all([
      listMeetingsWithAttendance(id),
      listMembers(id),
      listPolls(id),
    ]);
    setRoster(members.filter((m) => m.status === "active"));
    setItems(withAttendance);
    setPolls(chapterPolls);
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
        await loadSummary(eligible.chapter_id, "semester");
      } catch (error) {
        showApiError(error, "Couldn't load the secretary dashboard");
        setMembership(null);
      }
    };
    void init();
  }, [loadDashboard, loadSummary]);

  /**
   * Live poll updates (c162). Somebody else voting is the ONLY thing that moves a
   * tally without this screen doing anything, so it is the whole reason the
   * socket is here.
   *
   * The merge deliberately preserves the local `my_option_id`. The broadcast is
   * aggregate-only and cannot carry it -- one event goes to every member of the
   * chapter -- and keeping the local value is always right, because only your own
   * vote changes what you picked.
   *
   * Scoped to this chapter: the gateway subscribes per USER, so a member of two
   * chapters receives both chapters' polls on one socket. Without this check the
   * other chapter's votes would silently rewrite this screen.
   */
  useEffect(() => {
    if (chapterId === null) return;
    return chirpSocket.onEvent((event) => {
      if (!isPollEvent(event) || event.chapter_id !== chapterId) return;

      if (event.action === "deleted") {
        setPolls((prev) => (prev ?? []).filter((p) => p.id !== event.poll_id));
        return;
      }
      const incoming = event.poll;
      if (incoming === undefined) return;

      setPolls((prev) => {
        const current = prev ?? [];
        const existing = current.find((p) => p.id === incoming.id);
        if (existing === undefined) {
          // A poll opened by someone else. Nobody here has voted in it yet.
          return [{ ...incoming, my_option_id: null }, ...current];
        }
        return current.map((p) =>
          p.id === incoming.id ? { ...incoming, my_option_id: p.my_option_id } : p,
        );
      });
    });
  }, [chapterId]);

  if (membership === null) {
    return (
      <Screen title="Secretary" subtitle="Minutes, polls, and attendance">
        <EmptyState
          title="Secretary/president only"
          message="This dashboard is limited to the chapter's secretary or president."
        />
      </Screen>
    );
  }

  const activeRoster = roster ?? [];

  // The summary carries display_name but no photo; the roster call the attendance
  // sheet already makes carries both, so the avatar comes from there rather than
  // from widening the endpoint.
  const avatarFor = (userId: string): string | null =>
    activeRoster.find((m) => m.user_id === userId)?.avatar_url ?? null;

  // Ranked by absences, not alphabetically: the screen is answering "who is about to
  // owe a fine", so the person the secretary needs is at the top rather than wherever
  // their name happens to sort. The server orders by name because a server has no
  // opinion about which question is being asked.
  const rankedMembers = [...(summary?.members ?? [])].sort(
    (a, b) =>
      b.absent - a.absent ||
      b.excused - a.excused ||
      a.display_name.localeCompare(b.display_name),
  );

  const inputStyle = inputField(palette);

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
      // The totals' denominator counts meetings in the window; refetch rather than
      // try to add this one in locally (mirrors removeMeeting below).
      await loadSummary(chapterId, windowKey);
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
      // MeetingUpdate.meeting_date exists on the type, but this screen never edits
      // it — only minutes_md is patched here, so the attendance window's membership
      // can't shift as a side effect of this call and loadSummary need not rerun.
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

  const changeWindow = (key: WindowKey) => {
    if (chapterId === null || key === windowKey) return;
    setWindowKey(key);
    setSummary(null); // never show the previous window's numbers under the new label
    void loadSummary(chapterId, key);
  };

  /**
   * Delete is the one irreversible action on this screen: the meeting's attendance
   * rows go with it and it leaves the CSV the chapter hands to nationals. So it
   * confirms against the meeting's own title and date rather than a generic "are you
   * sure" — a secretary deleting the wrong duplicate is the mistake this whole card
   * exists to let them fix, and it must not become a second unfixable mistake.
   */
  const confirmDeleteMeeting = (meeting: MeetingOut) => {
    if (chapterId === null || deletingMeetingId !== null) return;
    confirmAction({
      title: "Delete this meeting?",
      message:
        `"${meeting.title}" on ${meetingDate(meeting.meeting_date)} and its attendance ` +
        "will be removed for everyone, and it will drop out of the CSV export. " +
        "This cannot be undone.",
      confirmLabel: "Delete",
      cancelLabel: "Keep",
      destructive: true,
      onConfirm: () => void removeMeeting(meeting),
    });
  };

  const removeMeeting = async (meeting: MeetingOut) => {
    if (chapterId === null || deletingMeetingId !== null) return; // double-submit guard
    setDeletingMeetingId(meeting.id);
    try {
      await deleteMeeting(chapterId, meeting.id);
      setItems((current) => (current ?? []).filter((it) => it.meeting.id !== meeting.id));
      // Close any editor still pointed at the meeting that no longer exists.
      setExpanded((current) => (current?.meetingId === meeting.id ? null : current));
      // The totals counted this meeting in their denominator; refetch rather than
      // try to subtract it locally.
      await loadSummary(chapterId, windowKey);
    } catch (error) {
      showApiError(error, "Couldn't delete meeting");
    } finally {
      setDeletingMeetingId(null);
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
        showAlert(
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

  const handleCreatePoll = async () => {
    if (chapterId === null) return;
    const question = newQuestion.trim();
    const options = newOptions.map((o) => o.trim()).filter((o) => o.length > 0);
    // Validated here as well as on the server so the common mistakes answer
    // instantly and in words, rather than as a 422 the form has to translate.
    if (question.length === 0) {
      setPollError("Give the poll a question.");
      return;
    }
    if (options.length < 2) {
      setPollError("A poll needs at least two options.");
      return;
    }
    if (new Set(options.map((o) => o.toLowerCase())).size !== options.length) {
      setPollError("Two options read the same, so the vote would split between them.");
      return;
    }

    setPollError(null);
    setCreatingPoll(true);
    try {
      const created = await createPoll(chapterId, { question, options });
      setPolls((prev) => [created, ...(prev ?? [])]);
      setNewQuestion("");
      setNewOptions(Array.from({ length: POLL_OPTION_SLOTS }, () => ""));
    } catch (error) {
      showApiError(error, "Couldn't open the poll");
    } finally {
      setCreatingPoll(false);
    }
  };

  /** Both vote and close return the whole poll, so the card re-renders from the
   * server's tally rather than from a guess made locally. */
  const replacePoll = (updated: PollOut) =>
    setPolls((prev) => (prev ?? []).map((p) => (p.id === updated.id ? updated : p)));

  const handleVote = async (pollId: string, optionId: string) => {
    if (chapterId === null) return;
    setBusyPollId(pollId);
    try {
      replacePoll(await castVote(chapterId, pollId, optionId));
    } catch (error) {
      showApiError(error, "Couldn't record your vote");
    } finally {
      setBusyPollId(null);
    }
  };

  const handleClosePoll = async (pollId: string) => {
    if (chapterId === null) return;
    setBusyPollId(pollId);
    try {
      replacePoll(await closePoll(chapterId, pollId));
    } catch (error) {
      showApiError(error, "Couldn't close the poll");
    } finally {
      setBusyPollId(null);
    }
  };

  return (
    <Screen title="Secretary" subtitle="Minutes, polls, and attendance">
      <View style={{ gap: spacing.xl }}>
        <View>
          <SectionHeader title="New meeting" caption="Title and date. Take minutes and attendance after" />
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
            title="Polls"
            caption="Open a vote. Results update as members tap"
          />
          <Card style={{ marginBottom: spacing.md }}>
            <View style={{ gap: spacing.lg }}>
              <View>
                <FieldLabel>Question</FieldLabel>
                <TextInput
                  value={newQuestion}
                  onChangeText={setNewQuestion}
                  placeholder="e.g. Approve the spring formal budget?"
                  placeholderTextColor={palette.inkFaint}
                  style={inputStyle}
                />
              </View>
              <View style={{ gap: spacing.sm }}>
                <FieldLabel>Options (blanks are ignored)</FieldLabel>
                {newOptions.map((value, index) => (
                  <TextInput
                    key={index}
                    value={value}
                    onChangeText={(text) =>
                      setNewOptions((prev) =>
                        prev.map((existing, i) => (i === index ? text : existing)),
                      )
                    }
                    placeholder={index === 0 ? "Yes" : index === 1 ? "No" : "Optional"}
                    placeholderTextColor={palette.inkFaint}
                    style={inputStyle}
                  />
                ))}
              </View>
              {pollError !== null ? (
                <AppText variant="caption" tone="danger">
                  {pollError}
                </AppText>
              ) : null}
              <Button
                label={creatingPoll ? "Opening…" : "Open poll"}
                onPress={() => void handleCreatePoll()}
                disabled={creatingPoll}
              />
            </View>
          </Card>

          {polls !== null && polls.length === 0 ? (
            <EmptyState
              title="No polls yet"
              message="Open one to take a vote during the meeting."
            />
          ) : (
            (polls ?? []).map((poll) => (
              <PollCard
                key={poll.id}
                poll={poll}
                busy={busyPollId === poll.id}
                onVote={(optionId) => void handleVote(poll.id, optionId)}
                onClose={() => void handleClosePoll(poll.id)}
              />
            ))
          )}
        </View>

        <View>
          <SectionHeader
            title="Attendance by member"
            caption={
              summary === null
                ? "Counting…"
                : `${summary.meetings_in_window} ${
                    summary.meetings_in_window === 1 ? "meeting" : "meetings"
                  } ${windowKey === "semester" ? "this semester" : "on record"}`
            }
            right={
              <View style={{ flexDirection: "row", gap: spacing.xs }}>
                {WINDOW_OPTIONS.map((option) => {
                  const selected = windowKey === option.key;
                  return (
                    <Pressable
                      key={option.key}
                      accessibilityRole="button"
                      accessibilityLabel={`Show ${option.label}`}
                      accessibilityState={{ selected }}
                      onPress={() => changeWindow(option.key)}
                      hitSlop={spacing.xs}
                      style={{
                        paddingHorizontal: spacing.md,
                        paddingVertical: spacing.xs,
                        borderRadius: radii.pill,
                        backgroundColor: selected ? palette.accentSoft : palette.surfaceAlt,
                      }}
                    >
                      <AppText
                        variant="micro"
                        style={{ color: selected ? palette.accent : palette.inkSecondary }}
                      >
                        {option.label}
                      </AppText>
                    </Pressable>
                  );
                })}
              </View>
            }
          />
          {summary === null ? (
            <AppText variant="caption" tone="tertiary">
              Loading attendance totals…
            </AppText>
          ) : summary.meetings_in_window === 0 ? (
            <EmptyState
              title="No meetings in this window"
              message={
                windowKey === "semester"
                  ? "Nothing has been logged this semester yet. Switch to All time to see earlier meetings."
                  : "Create a meeting and take attendance to start building the record."
              }
            />
          ) : (
            <Card>
              {rankedMembers.map((member, index) => {
                const unmarked = summary.meetings_in_window - member.recorded;
                const detail = [
                  `${member.present} present`,
                  `${member.excused} excused`,
                  // Not an absence, and must never be shown as one: nobody took a
                  // sheet for that meeting, or took one without this member on it.
                  unmarked > 0 ? `${unmarked} unmarked` : null,
                ]
                  .filter((part): part is string => part !== null)
                  .join(" · ");
                return (
                  <ListRow
                    key={member.user_id}
                    title={member.display_name}
                    subtitle={detail}
                    left={
                      <GradientAvatar
                        name={member.display_name}
                        size={32}
                        photoUrl={avatarFor(member.user_id)}
                      />
                    }
                    divider={index < rankedMembers.length - 1}
                    right={
                      <Chip
                        label={`${member.absent} missed`}
                        variant={member.absent > 0 ? "danger" : "neutral"}
                      />
                    }
                  />
                );
              })}
            </Card>
          )}
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
                        <Pressable
                          accessibilityRole="button"
                          accessibilityLabel={`Delete ${meeting.title}`}
                          accessibilityState={{ disabled: deletingMeetingId !== null }}
                          disabled={deletingMeetingId !== null}
                          onPress={() => confirmDeleteMeeting(meeting)}
                          hitSlop={spacing.sm}
                          // Pushed away from the two routine actions on purpose: the
                          // irreversible one should not sit a thumb-width from "Take
                          // attendance" during a meeting.
                          style={{ marginLeft: "auto" }}
                        >
                          <AppText
                            variant="bodyBold"
                            tone={deletingMeetingId === meeting.id ? "tertiary" : "danger"}
                          >
                            {deletingMeetingId === meeting.id ? "Deleting…" : "Delete"}
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
                              // GET /chapters/{id}/members joins the name (and photo) in;
                              // there is no GET /users/{id}, so this is the only real-data
                              // source for either.
                              const name = member.display_name;
                              const status = attendanceDraft[member.user_id] ?? "absent";
                              return (
                                <ListRow
                                  key={member.user_id}
                                  title={name}
                                  left={
                                    <GradientAvatar
                                      name={name}
                                      size={32}
                                      photoUrl={member.avatar_url}
                                    />
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

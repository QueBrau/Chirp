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

import { useCallback, useRef, useState } from "react";
import { useFocusEffect } from "expo-router";
import { Pressable, TextInput, View } from "react-native";

import { listMembers, myMemberships, type MemberOut, type MyMembershipOut } from "@/api/chapters";
import { useSession } from "@/auth";
import { ApiError } from "@/api/client";
import { Operation } from "@/api/operation";
import { currentIdentity, ownsIdentity, type AuthIdentity } from "@/auth/identity";
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
import { acceptPage, beginOlderPage, collectionPage, meetingOrder, mergePageRows, mergePollPage, pollOrder } from "@/lib/collectionPages";
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
// Matches the server's poll option text ceiling (c345).
const POLL_OPTION_MAX_LENGTH = 200;

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

/** One page of meetings, and of polls. Both grow with time; the server caps at 200.
 * Each meeting's attendance sheet still arrives WHOLE - only the lists page (c258). */
const MEETING_PAGE_SIZE = 50;
const POLL_PAGE_SIZE = 50;

function dashboardQuery(owner: AuthIdentity) {
  return {
    owner, active: true, focused: true, chapterId: null as string | null,
    meetings: collectionPage(), polls: collectionPage(),
    ownVoteKnown: new Set<string>(), summaryRequest: 0, windowKey: "semester" as WindowKey,
    pollRefresh: null as Operation | null, pollChanges: 0, pollWindowRows: POLL_PAGE_SIZE,
    pollInitial: null as Operation | null, pollOlder: null as Operation | null,
    pollsLoaded: false, pollRefreshRequested: false, pollReadGeneration: 0,
  };
}
type DashboardQuery = ReturnType<typeof dashboardQuery>;

export default function SecretaryScreen() {
  useSession();
  const renderOwner = currentIdentity();
  const queryRef = useRef(dashboardQuery(renderOwner));
  const renderQuery = queryRef.current;
  const currentQuery = (query: DashboardQuery) =>
    query === queryRef.current && query.owner === renderOwner && query.active && ownsIdentity(query.owner);
  const palette = useTheme();

  // undefined = /me/memberships hasn't resolved yet; null = signed-in user has
  // no secretary/president membership anywhere (role-gated screen — NO further
  // calls, the meetings/attendance endpoints would just 403); object = the
  // real membership driving every call below.
  const [membership, setMembership] = useState<MyMembershipOut | null | undefined>(undefined);
  // c313: failure is its own state, never conflated with "not eligible".
  const [loadFailed, setLoadFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
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
  /** A full page means older rows exist behind it (c258). */
  const [hasOlderMeetings, setHasOlderMeetings] = useState(false);
  const [hasOlderPolls, setHasOlderPolls] = useState(false);
  const [loadingOlderMeetings, setLoadingOlderMeetings] = useState(false);
  const [loadingOlderPolls, setLoadingOlderPolls] = useState(false);
  const [newQuestion, setNewQuestion] = useState("");
  const [newOptions, setNewOptions] = useState<string[]>(
    Array.from({ length: POLL_OPTION_SLOTS }, () => ""),
  );
  const [pollError, setPollError] = useState<string | null>(null);
  const [creatingPoll, setCreatingPoll] = useState(false);
  // Which poll has a request in flight, so only that card disables.
  const [busyPollId, setBusyPollId] = useState<string | null>(null);
  const [pollRefreshState, setPollRefreshState] = useState<"updating" | "incomplete" | null>(null);
  const [accessLost, setAccessLost] = useState(false);

  // Roster attendance totals (board c82) — one server call for the whole chapter.
  const [summary, setSummary] = useState<ChapterAttendanceSummary | null>(null);
  const [windowKey, setWindowKey] = useState<WindowKey>("semester");
  const [deletingMeetingId, setDeletingMeetingId] = useState<string | null>(null);

  const chapterId = membership?.chapter_id ?? null;

  const retireAccess = (query: DashboardQuery) => {
    if (!currentQuery(query)) return;
    query.active = false;
    query.pollRefresh?.cancel();
    query.pollInitial?.cancel(); query.pollOlder?.cancel();
    setItems(null); setPolls(null); setRoster(null); setSummary(null); setMembership(null);
    setAccessLost(true);
  };

  /** Re-read the visible poll window on every server-ready transition. Absolute
   * event tallies have no revision: overlapping activity invalidates this read,
   * rather than being assumed newer merely because its callback arrived later. */
  const refreshPollWindow = useCallback(async (query: DashboardQuery) => {
    if (!currentQuery(query) || query.chapterId === null) return;
    if (!query.pollsLoaded || query.pollRefresh !== null) { query.pollRefreshRequested = true; return; }
    const operation = new Operation({ timeoutMs: 15_000 }, query.owner);
    query.pollRefresh = operation;
    query.pollReadGeneration += 1;
    query.pollRefreshRequested = false;
    query.polls.pending = null; // Retire an older-page response before replacing its cursor.
    query.pollOlder?.cancel();
    setLoadingOlderPolls(false);
    setPollRefreshState("updating");
    const pages = Math.min(4, Math.max(1, Math.ceil(query.pollWindowRows / POLL_PAGE_SIZE)));
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        const change = query.pollChanges;
        query.pollRefreshRequested = false;
        let cursor: { before: string; beforeId: string } | undefined;
        let more = false;
        const rows: PollOut[] = [];
        for (let page = 0; page < pages; page++) {
          operation.assertCurrent();
          const batch = await operation.wait(listPolls(query.chapterId, { ...cursor, limit: POLL_PAGE_SIZE, operation }));
          if (!currentQuery(query)) return;
          if (batch.some(row => row.chapter_id !== query.chapterId)) throw new Error("Invalid poll window.");
          rows.push(...batch);
          const last = batch.at(-1);
          more = batch.length === POLL_PAGE_SIZE;
          if (last) cursor = { before: last.created_at, beforeId: last.id };
          if (!more) break;
        }
        if (query.pollChanges !== change || query.pollRefreshRequested) continue;
        const removed = new Set(query.polls.removed);
        query.polls.cursor = cursor ?? null;
        query.polls.more = more;
        query.pollWindowRows = rows.length;
        query.ownVoteKnown = new Set(rows.map(row => row.id));
        // Replace the covered window. Missed closes/deletes and personal ballots
        // come from the server; only irreversible deletion tombstones persist.
        setPolls(() => mergePageRows([], rows, row => row.id, pollOrder, removed));
        setHasOlderPolls(more);
        setPollRefreshState(null);
        return;
      }
      // At most one follow-up within the SAME deadline, even under constant votes.
      if (currentQuery(query)) setPollRefreshState("incomplete");
    } catch (error) {
      if (!currentQuery(query)) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404)) retireAccess(query);
      else setPollRefreshState("incomplete");
    } finally {
      operation.dispose();
      if (query.pollRefresh === operation) query.pollRefresh = null;
    }
  }, [renderOwner]);

  /**
   * Deliberately swallows its own errors instead of throwing to the caller: a failed
   * totals call must not take the minutes and attendance surfaces down with it, which
   * is what happens if this rejects inside init() below.
   */
  const loadSummary = useCallback(async (id: string, key: WindowKey, query = queryRef.current) => {
    if (!currentQuery(query) || query.chapterId !== id || query.windowKey !== key) return;
    const request = ++query.summaryRequest;
    try {
      const totals = await getAttendanceSummary(
        id,
        key === "semester" ? currentSemesterWindow(new Date()) : {},
      );
      if (currentQuery(query) && query.summaryRequest === request) setSummary(totals);
    } catch (error) {
      if (!currentQuery(query) || query.summaryRequest !== request) return;
      showApiError(error, "Couldn't load attendance totals");
    }
  }, [renderOwner]);

  /**
   * Three collection requests, whatever the chapter's history looks like (c156/c162).
   * This used to be listMeetings followed by getAttendance PER MEETING inside a
   * Promise.all — a semester of meetings meant a semester of requests every time the
   * dashboard opened, and it grew with the archive rather than with anything the
   * secretary asked for. The server returns most-recent-first, so there is no sort
   * here any more; the create path below still sorts, because a meeting logged for a
   * past date must not jump to the top.
   */
  const loadDashboard = useCallback(async (id: string, query: DashboardQuery) => {
    const operation = new Operation({ timeoutMs: 15_000 }, query.owner);
    query.pollInitial = operation;
    query.pollReadGeneration += 1;
    const pollChanges = query.pollChanges;
    try {
      const [withAttendance, members, chapterPolls] = await Promise.all([
        listMeetingsWithAttendance(id, { limit: MEETING_PAGE_SIZE }),
        listMembers(id),
        listPolls(id, { limit: POLL_PAGE_SIZE, operation }),
      ]);
      if (!currentQuery(query)) return;
      setRoster(members.filter((m) => m.status === "active"));
      const meeting = withAttendance.at(-1)?.meeting, poll = chapterPolls.at(-1);
      acceptPage(query.meetings, withAttendance.length, MEETING_PAGE_SIZE,
        meeting ? { before: meeting.meeting_date, beforeId: meeting.id } : null);
      acceptPage(query.polls, chapterPolls.length, POLL_PAGE_SIZE,
        poll ? { before: poll.created_at, beforeId: poll.id } : null);
      const removedMeetings = new Set(query.meetings.removed), removedPolls = new Set(query.polls.removed);
      const ownVoteKnown = new Set(query.ownVoteKnown);
      for (const row of chapterPolls) query.ownVoteKnown.add(row.id);
      setItems(current => mergePageRows(current, withAttendance, row => row.meeting.id, meetingOrder, removedMeetings));
      setHasOlderMeetings(query.meetings.more);
      setPolls(current => mergePollPage(current, chapterPolls, ownVoteKnown, removedPolls));
      setHasOlderPolls(query.polls.more);
      query.pollWindowRows = chapterPolls.length;
      query.pollsLoaded = true;
      if (query.pollChanges !== pollChanges) query.pollRefreshRequested = true;
      if (query.pollRefreshRequested) void refreshPollWindow(query);
    } finally {
      operation.cancel(); operation.dispose();
      if (query.pollInitial === operation) query.pollInitial = null;
    }
  }, [renderOwner, refreshPollWindow]);

  /** Append the page of meetings after the oldest held. Each sheet still arrives whole,
   * so the present/absent/excused counts below stay counts of the real sheet (c258). */
  const loadOlderMeetings = async () => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId) return;
    const request = beginOlderPage(query.meetings), cursor = query.meetings.cursor;
    if (request === null || cursor === null) return;
    setLoadingOlderMeetings(true);
    try {
      const older = await listMeetingsWithAttendance(chapterId, {
        ...cursor,
        limit: MEETING_PAGE_SIZE,
      });
      if (!currentQuery(query) || query.meetings.pending !== request) return;
      const meeting = older.at(-1)?.meeting;
      acceptPage(query.meetings, older.length, MEETING_PAGE_SIZE,
        meeting ? { before: meeting.meeting_date, beforeId: meeting.id } : null);
      const removed = new Set(query.meetings.removed);
      setHasOlderMeetings(query.meetings.more);
      setItems(current => mergePageRows(current, older, row => row.meeting.id, meetingOrder, removed));
    } catch (error) {
      if (!currentQuery(query) || query.meetings.pending !== request) return;
      showApiError(error, "Couldn't load earlier meetings");
    } finally {
      if (currentQuery(query) && query.meetings.pending === request) {
        query.meetings.pending = null;
        setLoadingOlderMeetings(false);
      }
    }
  };

  const loadOlderPolls = async () => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || query.pollRefresh !== null) return;
    const request = beginOlderPage(query.polls), cursor = query.polls.cursor;
    if (request === null || cursor === null) return;
    const operation = new Operation({ timeoutMs: 15_000 }, query.owner);
    query.pollOlder = operation;
    setLoadingOlderPolls(true);
    try {
      const older = await listPolls(chapterId, {
        ...cursor,
        limit: POLL_PAGE_SIZE,
        operation,
      });
      if (!currentQuery(query) || query.polls.pending !== request) return;
      const poll = older.at(-1);
      acceptPage(query.polls, older.length, POLL_PAGE_SIZE,
        poll ? { before: poll.created_at, beforeId: poll.id } : null);
      const ownVoteKnown = new Set(query.ownVoteKnown), removed = new Set(query.polls.removed);
      for (const row of older) query.ownVoteKnown.add(row.id);
      query.pollWindowRows += older.length;
      setHasOlderPolls(query.polls.more);
      setPolls(current => mergePollPage(current, older, ownVoteKnown, removed));
    } catch (error) {
      if (!currentQuery(query) || query.polls.pending !== request) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404)) retireAccess(query);
      else showApiError(error, "Couldn't load earlier polls");
    } finally {
      operation.dispose();
      if (query.pollOlder === operation) query.pollOlder = null;
      if (currentQuery(query) && query.polls.pending === request) {
        query.polls.pending = null;
        setLoadingOlderPolls(false);
      }
    }
  };

  useFocusEffect(useCallback(() => {
    const query = dashboardQuery(renderOwner);
    queryRef.current.active = false; queryRef.current.focused = false;
    queryRef.current = query;
    setMembership(undefined);
    setItems(null); setPolls(null); setRoster(null); setSummary(null);
    setHasOlderMeetings(false); setHasOlderPolls(false);
    setLoadingOlderMeetings(false); setLoadingOlderPolls(false);
    setCreatingMeeting(false); setCreatingPoll(false); setBusyPollId(null);
    setSavingMinutes(false); setSavingAttendance(false); setDeletingMeetingId(null);
    setExportingCsv(false);
    setExpanded(null); setMinutesDraft(""); setAttendanceDraft({});
    setNewTitle(""); setNewDateText(""); setNewQuestion("");
    setNewOptions(Array.from({ length: POLL_OPTION_SLOTS }, () => ""));
    setCreateError(null); setPollError(null); setWindowKey("semester");
    setPollRefreshState(null); setAccessLost(false);
    // Attach both listeners BEFORE any initial HTTP request, including when the
    // socket was already ready at mount. The initial fetch covers that ready state.
    const unsubscribeEvents = subscribePollEvents(query);
    const unsubscribeStatus = chirpSocket.onStatus(socketStatus => {
      if (!currentQuery(query) || socketStatus !== "open") return;
      if (!query.pollsLoaded) query.pollRefreshRequested = true;
      else void refreshPollWindow(query);
    });
    const init = async () => {
      setLoadFailed(false);
      try {
        const memberships = await myMemberships();
        if (!currentQuery(query)) return;
        const eligible =
          memberships.find((m) => m.role === "secretary" || m.role === "president") ?? null;
        setMembership(eligible);
        if (eligible === null) return; // role-gated: no meetings/attendance calls
        query.chapterId = eligible.chapter_id;
        await loadDashboard(eligible.chapter_id, query);
        await loadSummary(eligible.chapter_id, query.windowKey, query);
      } catch (error) {
        if (!currentQuery(query)) return;
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) { retireAccess(query); return; }
        showApiError(error, "Couldn't load the secretary dashboard");
        // c313: a FAILED load must not render as "Secretary/president only" -
        // that is the revoked-role lie c299 removed from treasurer.tsx. The
        // role is unknown here, not absent, so membership is left alone and
        // the failure gate below owns the render.
        setLoadFailed(true);
      }
    };
    void init();
    return () => { query.active = false; query.focused = false; query.pollRefresh?.cancel(); query.pollInitial?.cancel(); query.pollOlder?.cancel(); unsubscribeEvents(); unsubscribeStatus(); };
  }, [loadDashboard, loadSummary, refreshPollWindow, retryKey, renderOwner]));

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
  const subscribePollEvents = (query: DashboardQuery) => {
    return chirpSocket.onEvent((event) => {
      if (!currentQuery(query) || query.chapterId === null || !isPollEvent(event) || event.chapter_id !== query.chapterId) return;
      query.pollChanges += 1;

      if (event.action === "deleted") {
        query.polls.removed.add(event.poll_id);
        setPolls((prev) => (prev ?? []).filter((p) => p.id !== event.poll_id));
        return;
      }
      const incoming = event.poll;
      if (incoming === undefined || query.polls.removed.has(incoming.id)) return;

      setPolls((prev) => {
        const current = prev ?? [];
        const existing = current.find((p) => p.id === incoming.id);
        if (existing === undefined) {
          // A poll opened by someone else. Nobody here has voted in it yet.
          return [{ ...incoming, my_option_id: null }, ...current].sort(pollOrder);
        }
        return current.map((p) =>
          p.id === incoming.id ? { ...incoming, my_option_id: p.my_option_id } : p,
        );
      });
    });
  };

  const retryDashboard = () => {
    const query = renderQuery;
    if (query !== queryRef.current || !query.focused || query.owner !== renderOwner || !ownsIdentity(renderOwner)) return;
    setRetryKey(key => key + 1);
  };

  if (accessLost && queryRef.current.owner === renderOwner) return (
    <Screen title="Secretary" subtitle="Minutes, polls, and attendance">
      <EmptyState title="Dashboard unavailable" message="We couldn't verify access to this chapter. Try again to check."
        actionLabel="Try again" onAction={retryDashboard} />
    </Screen>
  );

  if (!currentQuery(queryRef.current)) return null;

  // c313: the failure gate outranks the role gate - on a failed load the role
  // is UNKNOWN, and "Secretary/president only" to a real secretary is the
  // revoked-role lie.
  if (loadFailed) {
    return (
      <Screen title="Secretary" subtitle="Minutes, polls, and attendance">
        <EmptyState
          title="Couldn't load the dashboard"
          message="Something went wrong reaching the server."
          actionLabel="Try again"
          onAction={retryDashboard}
        />
      </Screen>
    );
  }

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
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || creatingMeeting) return;
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
      if (!currentQuery(query)) return;
      setItems(current => mergePageRows(
        [{ meeting: created, attendance: [] as MeetingAttendanceOut[] }],
        current ?? [], row => row.meeting.id, meetingOrder,
      ));
      setNewTitle("");
      setNewDateText("");
      // The totals' denominator counts meetings in the window; refetch rather than
      // try to add this one in locally (mirrors removeMeeting below).
      await loadSummary(chapterId, query.windowKey, query);
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't create meeting");
    } finally {
      if (currentQuery(query)) setCreatingMeeting(false);
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
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || savingMinutes) return;
    setSavingMinutes(true);
    try {
      const trimmed = minutesDraft.trim();
      // MeetingUpdate.meeting_date exists on the type, but this screen never edits
      // it — only minutes_md is patched here, so the attendance window's membership
      // can't shift as a side effect of this call and loadSummary need not rerun.
      const updated = await updateMeeting(chapterId, meeting.id, {
        minutes_md: trimmed.length > 0 ? trimmed : null,
      });
      if (!currentQuery(query)) return;
      setItems((current) =>
        (current ?? []).map((it) => (it.meeting.id === meeting.id ? { ...it, meeting: updated } : it)),
      );
      setExpanded(null);
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't save minutes");
    } finally {
      if (currentQuery(query)) setSavingMinutes(false);
    }
  };

  const saveAttendance = async (meeting: MeetingOut) => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || savingAttendance) return;
    setSavingAttendance(true);
    try {
      const entries = Object.entries(attendanceDraft).map(([user_id, status]) => ({ user_id, status }));
      // putAttendance is a full-sheet bulk upsert — safe to re-save.
      const saved = await putAttendance(chapterId, meeting.id, { entries });
      if (!currentQuery(query)) return;
      setItems((current) =>
        (current ?? []).map((it) => (it.meeting.id === meeting.id ? { ...it, attendance: saved } : it)),
      );
      setExpanded(null);
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't save attendance");
    } finally {
      if (currentQuery(query)) setSavingAttendance(false);
    }
  };

  const changeWindow = (key: WindowKey) => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || key === query.windowKey) return;
    query.windowKey = key;
    setWindowKey(key);
    setSummary(null); // never show the previous window's numbers under the new label
    void loadSummary(chapterId, key, query);
  };

  /**
   * Delete is the one irreversible action on this screen: the meeting's attendance
   * rows go with it and it leaves the CSV the chapter hands to nationals. So it
   * confirms against the meeting's own title and date rather than a generic "are you
   * sure" — a secretary deleting the wrong duplicate is the mistake this whole card
   * exists to let them fix, and it must not become a second unfixable mistake.
   */
  const confirmDeleteMeeting = (meeting: MeetingOut) => {
    if (!currentQuery(renderQuery) || chapterId === null || deletingMeetingId !== null) return;
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
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || deletingMeetingId !== null) return;
    setDeletingMeetingId(meeting.id);
    try {
      await deleteMeeting(chapterId, meeting.id);
      if (!currentQuery(query)) return;
      query.meetings.removed.add(meeting.id);
      setItems((current) => (current ?? []).filter((it) => it.meeting.id !== meeting.id));
      // Close any editor still pointed at the meeting that no longer exists.
      setExpanded((current) => (current?.meetingId === meeting.id ? null : current));
      // The totals counted this meeting in their denominator; refetch rather than
      // try to subtract it locally.
      await loadSummary(chapterId, query.windowKey, query);
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't delete meeting");
    } finally {
      if (currentQuery(query)) setDeletingMeetingId(null);
    }
  };

  const exportCsv = async () => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || exportingCsv) return;
    setExportingCsv(true);
    try {
      const csv = await exportMeetingsCsv(chapterId);
      if (!currentQuery(query)) return;
      const today = new Date().toISOString().slice(0, 10);
      const filename = `${membership?.chapter_name ?? "chapter"} meetings ${today}`;
      try {
        await shareCsv(filename, csv);
      } catch {
        if (!currentQuery(query)) return;
        // expo-file-system/expo-sharing are newly-added native modules — until the
        // EAS dev build is rebuilt, shareCsv fails at native-module resolution even
        // though the CSV text above resolved fine. Surface that plainly instead of
        // letting it fall through as an unhandled rejection.
        showAlert(
          "Can't share yet",
          "The export worked, but sharing isn't available in this version of the app " +
            "yet. Ask whoever set up Chirp for your chapter to check for an update.",
        );
      }
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't export meeting minutes");
    } finally {
      if (currentQuery(query)) setExportingCsv(false);
    }
  };

  const handleCreatePoll = async () => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId || creatingPoll) return;
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
    const mutation = { changes: ++query.pollChanges, read: query.pollReadGeneration };
    try {
      const created = await createPoll(chapterId, { question, options });
      if (!currentQuery(query)) return;
      if (query.pollChanges !== mutation.changes || query.pollReadGeneration !== mutation.read) {
        // Neither an older POST body nor an aggregate event establishes ordering.
        query.pollChanges += 1;
        void refreshPollWindow(query);
      } else {
        const ownVoteKnown = new Set(query.ownVoteKnown), removed = new Set(query.polls.removed);
        query.ownVoteKnown.add(created.id); query.pollChanges += 1;
        setPolls(current => mergePollPage(current, [created], ownVoteKnown, removed));
      }
      setNewQuestion("");
      setNewOptions(Array.from({ length: POLL_OPTION_SLOTS }, () => ""));
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't open the poll");
    } finally {
      if (currentQuery(query)) setCreatingPoll(false);
    }
  };

  /** Both vote and close return the whole poll, so the card re-renders from the
   * server's tally rather than from a guess made locally. */
  const replacePoll = (updated: PollOut, query: DashboardQuery, mutation: { changes: number; read: number }) => {
    if (!currentQuery(query) || query.polls.removed.has(updated.id)) return;
    const ambiguous = query.pollChanges !== mutation.changes || query.pollReadGeneration !== mutation.read;
    query.pollChanges += 1;
    if (ambiguous) { void refreshPollWindow(query); return; }
    query.ownVoteKnown.add(updated.id);
    setPolls((prev) => (prev ?? []).map((p) => (p.id === updated.id ? updated : p)));
  };

  const handleVote = async (pollId: string, optionId: string) => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId) return;
    setBusyPollId(pollId);
    const mutation = { changes: ++query.pollChanges, read: query.pollReadGeneration };
    try {
      replacePoll(await castVote(chapterId, pollId, optionId), query, mutation);
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't record your vote");
    } finally {
      if (currentQuery(query)) setBusyPollId(null);
    }
  };

  const handleClosePoll = async (pollId: string) => {
    const query = renderQuery;
    if (!currentQuery(query) || chapterId === null || query.chapterId !== chapterId) return;
    setBusyPollId(pollId);
    const mutation = { changes: ++query.pollChanges, read: query.pollReadGeneration };
    try {
      replacePoll(await closePoll(chapterId, pollId), query, mutation);
    } catch (error) {
      if (!currentQuery(query)) return;
      showApiError(error, "Couldn't close the poll");
    } finally {
      if (currentQuery(query)) setBusyPollId(null);
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
          {pollRefreshState === "updating" ? <AppText variant="caption" tone="secondary">Refreshing poll results…</AppText> : null}
          {pollRefreshState === "incomplete" ? <EmptyState title="Poll updates may be incomplete"
            message="Some results could not be refreshed. Try again to check the current tally."
            actionLabel="Refresh polls" onAction={() => void refreshPollWindow(renderQuery)} /> : null}
          {hasOlderPolls ? <AppText variant="caption" tone="secondary">Showing a recent window. Earlier polls may include missed updates.</AppText> : null}
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
                    maxLength={POLL_OPTION_MAX_LENGTH}
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
          {hasOlderPolls ? (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Load earlier polls"
              accessibilityState={{ disabled: loadingOlderPolls, busy: loadingOlderPolls }}
              disabled={loadingOlderPolls}
              onPress={() => void loadOlderPolls()}
              style={({ pressed }) => ({
                alignSelf: "center",
                marginTop: spacing.md,
                paddingVertical: spacing.sm,
                paddingHorizontal: spacing.lg,
                borderRadius: radii.pill,
                backgroundColor: palette.surfaceAlt,
                opacity: loadingOlderPolls ? 0.6 : pressed ? 0.8 : 1,
              })}
            >
              <AppText variant="micro" tone="secondary">
                {loadingOlderPolls ? "Loading…" : "Load earlier polls"}
              </AppText>
            </Pressable>
          ) : null}
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
              {hasOlderMeetings ? (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Load earlier meetings"
                  accessibilityState={{ disabled: loadingOlderMeetings, busy: loadingOlderMeetings }}
                  disabled={loadingOlderMeetings}
                  onPress={() => void loadOlderMeetings()}
                  style={({ pressed }) => ({
                    alignSelf: "center",
                    marginTop: spacing.md,
                    paddingVertical: spacing.sm,
                    paddingHorizontal: spacing.lg,
                    borderRadius: radii.pill,
                    backgroundColor: palette.surfaceAlt,
                    opacity: loadingOlderMeetings ? 0.6 : pressed ? 0.8 : 1,
                  })}
                >
                  <AppText variant="micro" tone="secondary">
                    {loadingOlderMeetings ? "Loading…" : "Load earlier meetings"}
                  </AppText>
                </Pressable>
              ) : null}
            </View>
          )}
        </View>
      </View>
    </Screen>
  );
}

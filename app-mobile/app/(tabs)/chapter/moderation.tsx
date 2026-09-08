/**
 * Moderation (board c35/c78, App Store Guideline 1.2): lists open content reports,
 * lets an e-board moderator remove a reported chirp — the ONLY removal action the
 * backend actually supports (POST /moderation/chirps/{chirp_id}/remove, gated
 * `_require_any_eboard`) — and now closes reports for real via PATCH
 * /moderation/reports/{id} (c91). There is no endpoint to remove a reported post
 * or comment, and no endpoint at all for message_forward reports (E2EE — the
 * client would need to decrypt/forward plaintext, which the crypto layer doesn't
 * support yet, SPEC §6.7) — those reports get an explicit Dismiss action instead
 * of a Remove button that can't work, so the queue can still be emptied.
 *
 * Reachable from Orgs > Tools, gated to e-board roles via `roleMeta.eboard`
 * (chapter/index.tsx OrgToolsSegment) — c80 made that a named `moderation`
 * capability, so vice_president and historian see the tile now too, not only
 * treasurer/secretary/president. Re-checked here too, the same way
 * chapter/secretary.tsx / treasurer.tsx re-check their own role instead of
 * trusting the tile alone — a direct/deep-linked nav must still land on an
 * EmptyState rather than a screen full of would-be 403s.
 *
 * GET /moderation/reports has no chapter_id param — the backend scopes it to
 * every campus where the caller is active e-board (routers/moderation.py
 * list_reports), which may cover more than just `useOwnChapter()`'s single
 * chapter. That's the server's call to make, not this screen's.
 *
 * c91 shipped PATCH /moderation/reports/{id} with no client function and no call
 * site anywhere — this screen is the first thing that calls it. Removing a chirp now
 * resolves its report as "actioned" through the real endpoint instead of tracking
 * "handled" in local state for the session only, so a reload no longer resurrects
 * it as open.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, View } from "react-native";

import {
  listReports,
  removeChirp,
  resolveReport,
  type ContentReportOut,
  type ReportTargetType,
} from "@/api/moderation";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { AppText, Button, Card, Chip, EmptyState, Screen, SectionHeader } from "@/components";
import { confirmAction, showApiError } from "@/lib/alert";
import {
  acceptPage,
  beginOlderPage,
  collectionPage,
  mergePageRows,
  pageExhausted,
  reportOrder,
  type CollectionPage,
} from "@/lib/collectionPages";
import { radii, spacing, useTheme } from "@/theme";

type LoadState = "loading" | "loaded" | "error";

/** Compact relative age ("just now", "5h ago", "2d ago") — matches Home/Chirp. */
function age(iso: string): string {
  const hours = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 3_600_000));
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

const TARGET_LABELS: Record<ReportTargetType, string> = {
  chirp: "Chirp",
  post: "Post",
  comment: "Comment",
  message_forward: "Message",
  user: "User",
};

/** Only chirp removal is wired to a real endpoint — narrows target_id to string
 * so the Remove button below never has to fight a `string | null` type. */
function removableChirpId(report: ContentReportOut): string | null {
  return report.target_type === "chirp" ? report.target_id : null;
}

/** One page of the queue. The server caps at 200 (c258). */
const REPORT_PAGE_SIZE = 50;
/** Refill before the visible list runs dry, so a moderator clearing a page never
 * sees a spinner on an empty queue while more open reports sit behind it (c353). */
const REFILL_THRESHOLD = 10;

export default function ModerationScreen() {
  // Single-org world (useOwnChapter's own note): membership/roleMeta describe
  // the caller's one chapter. roleMeta is null while loading OR on a failed
  // fetch (same ambiguity the invite card on chapter/index.tsx already
  // accepts) — a real e-board member might see a brief "e-board only" flash
  // that self-corrects once roleMeta resolves, rather than ever showing this
  // dashboard to someone the backend would 403.
  const { membership, roleMeta } = useOwnChapter();
  const isEboard = membership !== null && (roleMeta?.eboard ?? []).includes(membership.role);

  const [state, setState] = useState<LoadState>("loading");
  const palette = useTheme();
  const [reports, setReports] = useState<ContentReportOut[]>([]);
  // One id in flight at a time, covers both "removing" and "dismissing" — a report
  // can only be leaving the open queue one way, never both.
  const [workingReportId, setWorkingReportId] = useState<string | null>(null);
  /** A full page means older reports exist behind it (c258). */
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // Cursor/exhaustion/tombstones live here, independent of the mutable `reports`
  // display array — resolving every visible report must not blank the cursor or
  // fake "queue exhausted" the way deriving both from `reports` used to (c353).
  const pageRef = useRef<CollectionPage>(collectionPage());

  const load = useCallback(async () => {
    setState("loading");
    // A refresh genuinely restarts the queue: drop any stale cursor/removed/pending
    // from the page this replaces, rather than resuming a half-reset one.
    pageRef.current = collectionPage();
    setLoadingOlder(false);
    try {
      // status=open is asked of the SERVER now, not filtered here. Filtering after
      // paging would let a page of resolved reports render an empty queue while real
      // open reports sat on later pages - a moderator told there is nothing to do while
      // work is outstanding (c258).
      const rows = await listReports({ status: "open", limit: REPORT_PAGE_SIZE });
      const oldest = rows.at(-1);
      acceptPage(pageRef.current, rows.length, REPORT_PAGE_SIZE,
        oldest ? { before: oldest.created_at, beforeId: oldest.id } : null);
      setReports(rows);
      setHasOlder(pageRef.current.more);
      setState("loaded");
    } catch (error) {
      setState("error");
      showApiError(error, "Couldn't load reports");
    }
  }, []);

  /** Append the page after the oldest report the STORED cursor (not the display
   * array) last saw — so resolving every visible report can't strand it. */
  const loadOlderReports = useCallback(async () => {
    const page = pageRef.current;
    const request = beginOlderPage(page), cursor = page.cursor;
    if (request === null || cursor === null) return;
    setLoadingOlder(true);
    try {
      const older = await listReports({
        status: "open",
        before: cursor.before,
        beforeId: cursor.beforeId,
        limit: REPORT_PAGE_SIZE,
      });
      if (pageRef.current !== page || page.pending !== request) return;
      const oldest = older.at(-1);
      acceptPage(page, older.length, REPORT_PAGE_SIZE,
        oldest ? { before: oldest.created_at, beforeId: oldest.id } : null);
      setHasOlder(page.more);
      setReports((current) => mergePageRows(current, older, (r) => r.id, reportOrder, page.removed));
    } catch (error) {
      if (pageRef.current !== page || page.pending !== request) return;
      showApiError(error, "Couldn't load earlier reports");
    } finally {
      if (pageRef.current === page && page.pending === request) {
        page.pending = null;
        setLoadingOlder(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!isEboard) return; // role-gated: no /moderation/reports call otherwise
    void load();
  }, [isEboard, load]);

  // Refill automatically once the visible list runs low, before it goes fully
  // empty in front of a moderator working through a page. `beginOlderPage`'s own
  // pending-guard collapses this with a concurrent manual "Load earlier" tap into
  // one in-flight request for free.
  const needsRefill = hasOlder && !loadingOlder && reports.length < REFILL_THRESHOLD;
  useEffect(() => {
    if (needsRefill) void loadOlderReports();
  }, [needsRefill, loadOlderReports]);

  if (!isEboard) {
    return (
      <Screen title="Moderation" subtitle="Reports and removal">
        <EmptyState
          title="E-board only"
          message="This dashboard is limited to your chapter's e-board."
        />
      </Screen>
    );
  }

  /**
   * Drop the report from the OPEN list once it's genuinely closed server-side,
   * rather than the old approach of tracking a local "handled" flag that a
   * reload would forget. This is the same query the initial load runs — a
   * fresh GET /moderation/reports would return this report at all, just with
   * status "actioned"/"dismissed" instead of "open" — so removing it here
   * keeps the screen in sync with what a reload will show without refetching.
   */
  const closeReport = (reportId: string) => {
    // Tombstoned so a straggling in-flight older/refill response that still
    // carries this id (a race against this client's own resolve) can't
    // resurrect it once merged via mergePageRows (c353).
    pageRef.current.removed.add(reportId);
    setReports((current) => current.filter((r) => r.id !== reportId));
  };

  const doRemove = async (report: ContentReportOut, chirpId: string) => {
    setWorkingReportId(report.id);
    try {
      // Reuse the reporter's own stated reason as the removal reason (v1
      // scaffolding, CONVENTIONS.md "functional-but-simple" for moderation)
      // rather than adding a second free-text field just for this.
      await removeChirp(chirpId, report.reason);
      // c78: c91's resolve endpoint existed with no caller anywhere. Removing
      // the chirp takes the content down; resolving the REPORT is what actually
      // empties the queue — the two were separate actions server-side and
      // this screen used to only ever do the first one.
      await resolveReport(report.id, "actioned", report.reason);
      closeReport(report.id);
    } catch (error) {
      showApiError(error, "Couldn't remove that chirp");
    } finally {
      setWorkingReportId(null);
    }
  };

  const doDismiss = async (report: ContentReportOut) => {
    setWorkingReportId(report.id);
    try {
      await resolveReport(report.id, "dismissed", "Reviewed, no action needed");
      closeReport(report.id);
    } catch (error) {
      showApiError(error, "Couldn't dismiss that report");
    } finally {
      setWorkingReportId(null);
    }
  };

  const confirmRemove = (report: ContentReportOut, chirpId: string) => {
    confirmAction({
      title: "Remove this chirp?",
      message: "This takes it down for everyone on the board. This can't be undone.",
      confirmLabel: "Remove",
      destructive: true,
      order: "confirm-first",
      onConfirm: () => void doRemove(report, chirpId),
    });
  };

  const confirmDismiss = (report: ContentReportOut) => {
    confirmAction({
      title: "Dismiss this report?",
      message: "Marks it reviewed with no action taken. It leaves the queue either way.",
      confirmLabel: "Dismiss",
      order: "confirm-first",
      onConfirm: () => void doDismiss(report),
    });
  };

  // Never true off a bare empty array alone — the array goes empty every time a
  // full page is resolved, well before the queue itself is exhausted (c353).
  const allClear = reports.length === 0 && pageExhausted(pageRef.current);

  return (
    <Screen title="Moderation" subtitle="Open reports across your e-board campuses" onRefresh={load}>
      {state === "loading" ? (
        <EmptyState title="Loading reports..." />
      ) : state === "error" ? (
        <EmptyState
          title="Couldn't load reports"
          message="Something went wrong reaching the server."
          actionLabel="Try again"
          onAction={() => void load()}
        />
      ) : allClear ? (
        <EmptyState title="All clear" message="No open reports right now." />
      ) : reports.length === 0 ? (
        <EmptyState title="Loading more reports" />
      ) : (
        <View style={{ gap: spacing.md }}>
          {/* "N+" while more pages exist, because reports.length is then the size of
              the PAGE, not of the queue. Claiming the page count as the queue count is
              the truncation-as-fact bug this card exists to remove - the same shape as
              the comments header and the ledger balance before it. */}
          <SectionHeader
            title="Open reports"
            caption={`${reports.length}${hasOlder ? "+" : ""} waiting`}
          />
          {reports.map((report) => {
            const chirpId = removableChirpId(report);
            const working = workingReportId === report.id;
            return (
              <Card key={report.id}>
                <View style={{ gap: spacing.sm }}>
                  <View
                    style={{
                      flexDirection: "row",
                      alignItems: "center",
                      justifyContent: "space-between",
                    }}
                  >
                    <Chip label={TARGET_LABELS[report.target_type]} />
                    <AppText variant="caption" tone="tertiary">
                      {age(report.created_at)}
                    </AppText>
                  </View>
                  <AppText>{report.reason}</AppText>
                  {report.forwarded_plaintext !== null ? (
                    <AppText variant="caption" tone="secondary" numberOfLines={3}>
                      {report.forwarded_plaintext}
                    </AppText>
                  ) : null}
                  {chirpId === null ? (
                    <AppText variant="caption" tone="tertiary">
                      Removal isn't available for {TARGET_LABELS[report.target_type].toLowerCase()}{" "}
                      reports yet. Dismiss it once you've reviewed it.
                    </AppText>
                  ) : null}

                  <View style={{ gap: spacing.xs }}>
                    {chirpId !== null ? (
                      <Button
                        label={working ? "Working..." : "Remove chirp"}
                        variant="destructive"
                        disabled={working}
                        onPress={() => confirmRemove(report, chirpId)}
                      />
                    ) : null}
                    <Button
                      label={working ? "Working..." : "Dismiss"}
                      variant="ghost"
                      disabled={working}
                      onPress={() => confirmDismiss(report)}
                    />
                  </View>
                </View>
              </Card>
            );
          })}
          {hasOlder ? (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Load earlier reports"
              accessibilityState={{ disabled: loadingOlder, busy: loadingOlder }}
              disabled={loadingOlder}
              onPress={() => void loadOlderReports()}
              style={({ pressed }) => ({
                alignSelf: "center",
                marginTop: spacing.md,
                paddingVertical: spacing.sm,
                paddingHorizontal: spacing.lg,
                borderRadius: radii.pill,
                backgroundColor: palette.surfaceAlt,
                opacity: loadingOlder ? 0.6 : pressed ? 0.8 : 1,
              })}
            >
              <AppText variant="micro" tone="secondary">
                {loadingOlder ? "Loading…" : "Load earlier reports"}
              </AppText>
            </Pressable>
          ) : null}
        </View>
      )}
    </Screen>
  );
}

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

import { useCallback, useEffect, useState } from "react";
import { View } from "react-native";

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
import { spacing } from "@/theme";

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
  const [reports, setReports] = useState<ContentReportOut[]>([]);
  // One id in flight at a time, covers both "removing" and "dismissing" — a report
  // can only be leaving the open queue one way, never both.
  const [workingReportId, setWorkingReportId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    try {
      const all = await listReports();
      setReports(all.filter((r) => r.status === "open"));
      setState("loaded");
    } catch (error) {
      setState("error");
      showApiError(error, "Couldn't load reports");
    }
  }, []);

  useEffect(() => {
    if (!isEboard) return; // role-gated: no /moderation/reports call otherwise
    void load();
  }, [isEboard, load]);

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
      await resolveReport(report.id, "dismissed", "Reviewed — no action needed");
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

  return (
    <Screen title="Moderation" subtitle="Open reports across your e-board campuses">
      {state === "loading" ? (
        <EmptyState title="Loading reports..." />
      ) : state === "error" ? (
        <EmptyState
          title="Couldn't load reports"
          message="Something went wrong reaching the server."
          actionLabel="Try again"
          onAction={() => void load()}
        />
      ) : reports.length === 0 ? (
        <EmptyState title="All clear" message="No open reports right now." />
      ) : (
        <View style={{ gap: spacing.md }}>
          <SectionHeader title="Open reports" caption={`${reports.length} waiting`} />
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
                      reports yet — dismiss it once you've reviewed it.
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
        </View>
      )}
    </Screen>
  );
}

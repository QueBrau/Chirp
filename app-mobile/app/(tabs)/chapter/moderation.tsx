/**
 * Moderation (board c35, App Store Guideline 1.2): lists open content reports
 * and lets an e-board moderator remove a reported yak — the ONLY removal
 * action the backend actually supports (POST /moderation/yaks/{yak_id}/remove,
 * gated `_require_any_eboard`). There is no endpoint to remove a reported post
 * or comment, and no endpoint at all for message_forward reports (E2EE — the
 * client would need to decrypt/forward plaintext, which the crypto layer
 * doesn't support yet, SPEC §6.7) — those reports render read-only with an
 * explicit "not available" note instead of a button that can't work.
 *
 * Reachable from Orgs > Tools, gated to e-board roles via `roleMeta.eboard`
 * (chapter/index.tsx OrgToolsSegment). Re-checked here too, the same way
 * chapter/secretary.tsx / treasurer.tsx re-check their own role instead of
 * trusting the tile alone — a direct/deep-linked nav must still land on an
 * EmptyState rather than a screen full of would-be 403s.
 *
 * GET /moderation/reports has no chapter_id param — the backend scopes it to
 * every campus where the caller is active e-board (routers/moderation.py
 * list_reports), which may cover more than just `useOwnChapter()`'s single
 * chapter. That's the server's call to make, not this screen's.
 *
 * Report status has no transition endpoint anywhere in the API (no PATCH/
 * dismiss route) — removing a yak does not flip its report(s) to "actioned".
 * So a removed yak's report(s) are tracked as "handled" locally for this
 * session only; a fresh load will show them as "open" again until that
 * backend gap is closed (out of scope here — SCOPE only covers feed.py).
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, View } from "react-native";

import { ApiError } from "@/api/client";
import {
  listReports,
  removeYak,
  type ContentReportOut,
  type ReportTargetType,
} from "@/api/moderation";
import { useOwnChapter } from "@/org/OwnChapterProvider";
import { AppText, Button, Card, Chip, EmptyState, Screen, SectionHeader } from "@/components";
import { spacing } from "@/theme";

type LoadState = "loading" | "loaded" | "error";

/** ApiError carries a server-provided `.detail`; anything else gets a generic fallback. */
function showApiError(error: unknown, title: string): void {
  const message = error instanceof ApiError ? error.detail : "Something went wrong. Try again.";
  Alert.alert(title, message);
}

/** Compact relative age ("just now", "5h ago", "2d ago") — matches Home/Yak. */
function age(iso: string): string {
  const hours = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 3_600_000));
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

const TARGET_LABELS: Record<ReportTargetType, string> = {
  yak: "Yak",
  post: "Post",
  comment: "Comment",
  message_forward: "Message",
  user: "User",
};

/** Only yak removal is wired to a real endpoint — narrows target_id to string
 * so the Remove button below never has to fight a `string | null` type. */
function removableYakId(report: ContentReportOut): string | null {
  return report.target_type === "yak" ? report.target_id : null;
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
  const [removedYakIds, setRemovedYakIds] = useState<Set<string>>(new Set());
  const [removingReportId, setRemovingReportId] = useState<string | null>(null);

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

  const doRemove = async (report: ContentReportOut, yakId: string) => {
    setRemovingReportId(report.id);
    try {
      // Reuse the reporter's own stated reason as the removal reason (v1
      // scaffolding, CONVENTIONS.md "functional-but-simple" for moderation)
      // rather than adding a second free-text field just for this.
      await removeYak(yakId, report.reason);
      setRemovedYakIds((current) => new Set(current).add(yakId));
    } catch (error) {
      showApiError(error, "Couldn't remove that yak");
    } finally {
      setRemovingReportId(null);
    }
  };

  const confirmRemove = (report: ContentReportOut, yakId: string) => {
    Alert.alert(
      "Remove this yak?",
      "This takes it down for everyone on the board. This can't be undone.",
      [
        { text: "Remove", style: "destructive", onPress: () => void doRemove(report, yakId) },
        { text: "Cancel", style: "cancel" },
      ],
    );
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
            const yakId = removableYakId(report);
            const alreadyRemoved = yakId !== null && removedYakIds.has(yakId);
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

                  {yakId !== null ? (
                    alreadyRemoved ? (
                      <Chip label="Removed" variant="success" />
                    ) : (
                      <Button
                        label={removingReportId === report.id ? "Removing..." : "Remove yak"}
                        variant="destructive"
                        disabled={removingReportId === report.id}
                        onPress={() => confirmRemove(report, yakId)}
                      />
                    )
                  ) : (
                    <AppText variant="caption" tone="tertiary">
                      Removal isn't available for {TARGET_LABELS[report.target_type].toLowerCase()}{" "}
                      reports yet.
                    </AppText>
                  )}
                </View>
              </Card>
            );
          })}
        </View>
      )}
    </Screen>
  );
}

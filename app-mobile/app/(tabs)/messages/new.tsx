/**
 * New conversation picker (board c273): multi-select over the caller's own
 * chapter roster, active members only, self excluded. 1 pick -> kind "dm",
 * 2+ -> kind "group" with an optional title. POSTs createConversation() —
 * its first real call site; src/api/messages.ts has carried it, uncalled,
 * since it was added.
 *
 * No FAB: DESIGN.md §7 is explicit ("One FAB, Home only") and there is no
 * header-action slot on the shared Screen component to add one to. The entry
 * point instead lives in messages/index.tsx as a header-adjacent ghost pill,
 * the same shape as profile/index.tsx's local EditLayoutToggle.
 *
 * Honest-limit rule: creating a conversation here does NOT unlock sending —
 * src/crypto/signal.ts is still a typed stub (every export throws
 * TODO(milestone-3)), so sendMessage() has nothing that can call it. The
 * thread screen (app/(tabs)/messages/[id].tsx) already discloses this at the
 * composer ("Sending unlocks with E2EE (milestone 4)"); this screen repeats
 * that EXACT line rather than inventing a second, differently-worded claim
 * about encryption or sending (this repo has shipped that mistake twice).
 */

import { useRouter } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { TextInput, View } from "react-native";

import { Feather } from "@expo/vector-icons";

import { listMembers, type MemberOut } from "@/api/chapters";
import { ApiError } from "@/api/client";
import { createConversation, type ConversationKind } from "@/api/messages";
import { useSession } from "@/auth";
import { showAlert } from "@/lib/alert";
import { roleLabel } from "@/lib/roleTerms";
import { AppText, Button, Card, EmptyState, GradientAvatar, ListRow, Screen } from "@/components";
import { inputField, radii, spacing, typography, useTheme } from "@/theme";

/**
 * Human sentences for the codes routers/messages.py:126 can 403/404 with.
 * Never surface a raw server code like "recipient_not_reachable" verbatim
 * (c164 nit) — apiErrorMessage()/showApiError() would do exactly that for an
 * unmapped ApiError, so known codes are mapped here first and everything
 * else falls back to a generic line.
 */
function createConversationErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.detail) {
      case "not_a_member":
        return "You need to be an active member of this chapter to start this conversation.";
      case "recipient_not_reachable":
        return "Couldn't start that conversation with everyone you picked.";
      case "user_not_found":
        return "One of the people you picked couldn't be found. Try again.";
      default:
        break;
    }
  }
  return "Something went wrong. Try again.";
}

/** Selection state for a roster row: filled accent check when picked, an outline ring otherwise. */
function SelectionMark({ selected }: { selected: boolean }) {
  const palette = useTheme();
  return (
    <View
      style={{
        width: 24,
        height: 24,
        borderRadius: radii.pill,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: selected ? palette.accent : "transparent",
        borderWidth: selected ? 0 : 2,
        borderColor: palette.border,
      }}
    >
      {selected ? <Feather name="check" size={14} color={palette.onAccent} /> : null}
    </View>
  );
}

export default function NewConversationScreen() {
  const router = useRouter();
  const palette = useTheme();
  const { status: sessionStatus, user, memberships } = useSession();
  // Single-org world (the same derivation OwnChapterProvider itself does):
  // memberships[0] is the caller's only chapter. That provider isn't in
  // scope here — it only wraps the chapter/* stack (chapter/_layout.tsx) —
  // so this reads the session's own embedded memberships directly instead.
  const membership = memberships[0] ?? null;
  const chapterId = membership?.chapter_id ?? null;

  const [members, setMembers] = useState<MemberOut[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [groupTitle, setGroupTitle] = useState("");
  const [submitting, setSubmitting] = useState(false);
  /** The roster fetch failed. Distinct from a chapter with nobody to add (c317). */
  const [loadFailed, setLoadFailed] = useState(false);
  // A ref, not the state above: two taps in the same frame both run against
  // the SAME render's closure, where `submitting` is still false, so the state
  // check cannot see the first tap. The ref mutates synchronously and can.
  const submittingRef = useRef(false);

  useEffect(() => {
    if (chapterId === null) {
      setMembers(null);
      return;
    }
    // NOT `.catch(() => setMembers([]))` (c317), and note the justification that
    // used to sit here — "matches the repo pattern elsewhere in this stack" — was
    // the identical sentence c299 removed from chapter/members.tsx. An empty roster
    // is the server saying this chapter has nobody to add; a failed request says
    // nothing at all, and rendering both as "No one to add yet" tells a member of
    // an eight-person chapter they have no one to talk to.
    setLoadFailed(false);
    listMembers(chapterId)
      .then(setMembers)
      .catch(() => setLoadFailed(true));
  }, [chapterId]);

  // Session-status gating (same rule as chapter/members.tsx): a real member's
  // roster must never flash "no one to add" while the session or roster are
  // still resolving.
  // `!loadFailed &&` matters: on failure `members` stays null, and without this the
  // screen would sit on "Loading roster..." forever instead of saying what happened.
  const loading =
    !loadFailed && (sessionStatus === "loading" || (membership !== null && members === null));

  const roster = (members ?? []).filter(
    (member) => member.status === "active" && member.user_id !== user?.id,
  );

  const toggle = (userId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  };

  const kind: ConversationKind = selected.size >= 2 ? "group" : "dm";

  const submit = async () => {
    // Re-checked here, not just via the Button's disabled prop: a fast
    // double-tap queues the second onPress before the first setSubmitting(true)
    // has re-rendered, so both would see `disabled={false}` AND a stale
    // `submitting === false`. Only the synchronous ref stops the second POST.
    if (selected.size === 0 || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      const conversation = await createConversation({
        chapter_id: chapterId,
        kind,
        title: kind === "group" && groupTitle.trim().length > 0 ? groupTitle.trim() : null,
        member_user_ids: [...selected],
      });
      // replace(), not push(): back from the new thread should return to the
      // conversation list, not to this picker.
      router.replace(`/messages/${conversation.id}`);
    } catch (error) {
      showAlert("Couldn't start that conversation", createConversationErrorMessage(error));
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  return (
    <Screen
      title="New conversation"
      subtitle={loading ? undefined : "Pick chapter members to start with"}
    >
      {loading ? (
        <EmptyState title="Loading roster..." />
      ) : loadFailed ? (
        // Ahead of the "No one to add yet" branch below, because that branch is what
        // the failure used to fall through into. The state alone does not fix this
        // screen — without a branch that reads it, the roster still renders the empty
        // copy and the fix is invisible. Caught by the live pass, not by tsc.
        <EmptyState
          title="Couldn't load the roster"
          message="Check your connection and try again. This isn't a statement that your chapter has nobody to message."
        />
      ) : chapterId === null ? (
        <EmptyState
          title="No chapter yet"
          message="Join a chapter to start a conversation with its members."
        />
      ) : roster.length === 0 ? (
        <EmptyState title="No one to add yet" message="Active chapter members will show up here." />
      ) : (
        <View style={{ gap: spacing.lg }}>
          <Card>
            {roster.map((member, index) => {
              const isSelected = selected.has(member.user_id);
              const label = member.display_name.length > 0 ? member.display_name : member.user_id;
              return (
                <ListRow
                  key={member.id}
                  title={label}
                  subtitle={roleLabel(member.role)}
                  left={<GradientAvatar name={label} size={40} photoUrl={member.avatar_url} />}
                  right={<SelectionMark selected={isSelected} />}
                  divider={index < roster.length - 1}
                  onPress={() => toggle(member.user_id)}
                />
              );
            })}
          </Card>

          {kind === "group" ? (
            <Card>
              <AppText variant="micro" tone="secondary" style={{ marginBottom: spacing.xs }}>
                Group name (optional)
              </AppText>
              <TextInput
                value={groupTitle}
                onChangeText={setGroupTitle}
                placeholder="e.g. Pledge class 2027"
                placeholderTextColor={palette.inkFaint}
                style={inputField(palette)}
              />
            </Card>
          ) : null}

          <Button
            label={submitting ? "Starting..." : "Start conversation"}
            onPress={() => void submit()}
            disabled={selected.size === 0 || submitting}
          />

          <View
            style={{
              flexDirection: "row",
              alignItems: "center",
              justifyContent: "center",
              gap: spacing.xs,
            }}
          >
            <Feather name="lock" size={typography.caption.fontSize} color={palette.inkFaint} />
            <AppText variant="caption" tone="tertiary">
              Sending unlocks with E2EE (milestone 4)
            </AppText>
          </View>
        </View>
      )}
    </Screen>
  );
}
